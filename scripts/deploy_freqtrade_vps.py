#!/usr/bin/env python3
import argparse
import shlex
import sys
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path


try:
    import paramiko
except ImportError:
    print("Missing dependency: paramiko. Install with: python -m pip install paramiko")
    sys.exit(1)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEPLOY_DIR = "/opt/freqtrade"
REQUIRED_ENV_KEYS = (
    "DEPLOY_HOST",
    "DEPLOY_USER",
    "DEPLOY_PASS",
    "FREQTRADE__EXCHANGE__KEY",
    "FREQTRADE__EXCHANGE__SECRET",
)
REQUIRED_CLEANUP_KEYS = (
    "DEPLOY_HOST",
    "DEPLOY_USER",
    "DEPLOY_PASS",
)


def load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def require_keys(env: dict[str, str], keys: Iterable[str]) -> None:
    missing = [key for key in keys if not env.get(key)]
    if missing:
        raise RuntimeError(f"Missing required env keys: {', '.join(missing)}")


def connect_ssh(host: str, username: str, password: str, port: int) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # noqa: S507
    client.connect(hostname=host, username=username, password=password, port=port, timeout=15)
    return client


def run_remote(client: paramiko.SSHClient, command: str) -> None:
    _stdin, stdout, stderr = client.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="ignore")
    err = stderr.read().decode("utf-8", errors="ignore")
    if exit_status != 0:
        raise RuntimeError(f"Remote command failed: {command}\n{out}\n{err}")


def run_remote_capture(client: paramiko.SSHClient, command: str) -> str:
    _stdin, stdout, stderr = client.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="ignore")
    err = stderr.read().decode("utf-8", errors="ignore")
    if exit_status != 0:
        raise RuntimeError(f"Remote command failed: {command}\n{out}\n{err}")
    return out


def install_system_packages(client: paramiko.SSHClient) -> None:
    if _remote_command_exists(client, "apt-get"):
        run_remote(client, "apt-get update -y")
        run_remote(
            client,
            "apt-get install -y git python3 python3-venv python3-pip "
            "build-essential libtool pkg-config curl wget",
        )
        run_remote(client, "apt-get install -y libta-lib0 libta-lib-dev || true")
        return
    if _remote_command_exists(client, "dnf"):
        run_remote(
            client,
            "dnf install -y git python3 python3-pip python3-virtualenv "
            "gcc gcc-c++ make libtool pkgconfig curl wget",
        )
        run_remote(client, "dnf install -y ta-lib ta-lib-devel || true")
        return
    if _remote_command_exists(client, "yum"):
        run_remote(
            client,
            "yum install -y git python3 python3-pip python3-virtualenv "
            "gcc gcc-c++ make libtool pkgconfig curl wget",
        )
        run_remote(client, "yum install -y ta-lib ta-lib-devel || true")
        return
    raise RuntimeError("No supported package manager found (apt-get/dnf/yum).")


def _remote_command_exists(client: paramiko.SSHClient, command: str) -> bool:
    _stdin, stdout, _stderr = client.exec_command(f"command -v {command}")
    exit_status = stdout.channel.recv_exit_status()
    return exit_status == 0


def parse_cleanup_paths(raw: str) -> list[str]:
    if not raw:
        return []
    normalized = raw.replace("\n", ",").replace(";", ",")
    return [entry.strip() for entry in normalized.split(",") if entry.strip()]


def discover_ai_iteration_paths(client: paramiko.SSHClient, deploy_dir: str) -> list[str]:
    strategies_dir = f"{deploy_dir}/user_data/strategies"
    ai_paths: list[str] = []
    try:
        listing = run_remote_capture(client, f"ls -1 {shlex.quote(strategies_dir)}")
    except RuntimeError:
        listing = ""
    for line in listing.splitlines():
        name = line.strip()
        if not name or not name.endswith(".py"):
            continue
        lowered = name.lower()
        if any(token in lowered for token in ("ai", "iteration", "valuescan")):
            ai_paths.append(f"user_data/strategies/{name}")

    user_data_dir = f"{deploy_dir}/user_data"
    try:
        listing = run_remote_capture(client, f"ls -1 {shlex.quote(str(user_data_dir))}")
    except RuntimeError:
        listing = ""
    for line in listing.splitlines():
        name = line.strip()
        if not name or "valuescan" not in name.lower():
            continue
        lowered = name.lower()
        if "localstorage" in lowered:
            continue
        if any(token in lowered for token in ("tuning", "feedback", "iteration", "ai")):
            ai_paths.append(f"user_data/{name}")

    ai_paths.append("freqtrade/valuescan_api")

    return sorted(set(ai_paths))


def render_cornna_nginx(domain: str, web_root: str, monitor_port: int) -> str:
    return "\n".join(
        [
            "server {",
            "  listen 80;",
            f"  server_name {domain};",
            "  return 301 https://$host$request_uri;",
            "}",
            "",
            "server {",
            "  listen 443 ssl http2;",
            f"  server_name {domain};",
            f"  root {web_root};",
            "  index index.html;",
            f"  ssl_certificate /etc/ssl/certs/{domain}.cer;",
            f"  ssl_certificate_key /etc/ssl/private/{domain}.key;",
            "",
            "  location /api/ {",
            f"    proxy_pass http://127.0.0.1:{monitor_port};",
            "    proxy_http_version 1.1;",
            "    proxy_set_header Host $host;",
            "    proxy_set_header X-Real-IP $remote_addr;",
            "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "  }",
            "",
            "  location / {",
            "    try_files $uri $uri/ =404;",
            "  }",
            "}",
            "",
        ]
    )


def remove_remote_paths(
    client: paramiko.SSHClient,
    deploy_dir: str,
    rel_paths: Iterable[str],
) -> None:
    for rel_path in rel_paths:
        if rel_path.startswith("/"):
            raise RuntimeError(
                f"Cleanup path must be relative to DEPLOY_DIR: {rel_path}"
            )
        full_path = f"{deploy_dir.rstrip('/')}/{rel_path.lstrip('/')}"
        run_remote(client, f"rm -rf {shlex.quote(full_path)}")


def build_payload_tarball(dest: Path) -> None:
    ai_iteration_dir = ROOT / "freqtrade" / "ai_iteration"
    if not ai_iteration_dir.is_dir():
        raise RuntimeError(f"Missing AI iteration module at {ai_iteration_dir}")

    strategy_files = [
        ROOT / "user_data" / "strategies" / "CcxtSegmentedStrategyAI.py",
    ]
    config_files = [
        ROOT / "user_data" / "config_ccxt_main.json",
        ROOT / "user_data" / "config_ccxt_alt.json",
    ]
    anomaly_config_example = ROOT / "config_examples" / "anomaly_monitor.json"
    monitor_script = ROOT / "scripts" / "ccxt_monitor_api.py"
    replay_script = ROOT / "scripts" / "ai_iteration_replay.py"
    trade_etl_script = ROOT / "scripts" / "etl_prepare_trade_data.py"
    anomaly_script = ROOT / "scripts" / "anomaly_monitor.py"
    telegram_script = ROOT / "scripts" / "telegram_notify.py"
    strategy_iter_script = ROOT / "scripts" / "ai_strategy_iterate.py"

    for path in [
        *strategy_files,
        *config_files,
        anomaly_config_example,
        monitor_script,
        replay_script,
        trade_etl_script,
        strategy_iter_script,
        anomaly_script,
        telegram_script,
    ]:
        if not path.exists():
            raise RuntimeError(f"Missing required file: {path}")

    with tarfile.open(dest, "w:gz") as tar:
        tar.add(ai_iteration_dir, arcname="freqtrade/ai_iteration")
        for strategy_file in strategy_files:
            tar.add(strategy_file, arcname=f"user_data/strategies/{strategy_file.name}")
        for config_file in config_files:
            tar.add(config_file, arcname=f"user_data/{config_file.name}")
        tar.add(anomaly_config_example, arcname="config_examples/anomaly_monitor.json")
        tar.add(monitor_script, arcname="scripts/ccxt_monitor_api.py")
        tar.add(replay_script, arcname="scripts/ai_iteration_replay.py")
        tar.add(trade_etl_script, arcname="scripts/etl_prepare_trade_data.py")
        tar.add(strategy_iter_script, arcname="scripts/ai_strategy_iterate.py")
        tar.add(anomaly_script, arcname="scripts/anomaly_monitor.py")
        tar.add(telegram_script, arcname="scripts/telegram_notify.py")


def write_remote_file(
    sftp: paramiko.SFTPClient,
    path: str,
    content: str,
    mode: int = 0o600,
) -> None:
    with sftp.open(path, "w") as handle:
        handle.write(content)
    sftp.chmod(path, mode)


def service_name_from_config(config_path: str) -> str:
    name = Path(config_path).stem.replace("config_", "").replace("_", "-")
    return f"freqtrade-{name}"


def main() -> None:  # noqa: C901
    parser = argparse.ArgumentParser(description="Deploy Freqtrade to VPS.")
    parser.add_argument("--env-file", default="deploy.env", help="Path to env file.")
    parser.add_argument("--cleanup", action="store_true", help="Remove AI strategy files on VPS.")
    parser.add_argument(
        "--cleanup-ai",
        action="store_true",
        help="Discover and remove AI iteration strategy files on VPS.",
    )
    parser.add_argument(
        "--no-monitor",
        action="store_true",
        help="Skip installing the AI monitor API service.",
    )
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only remove AI strategy files and exit.",
    )
    parser.add_argument(
        "--allow-empty-keys",
        action="store_true",
        help="Skip requiring exchange keys (dry-run only).",
    )
    args = parser.parse_args()

    env_path = Path(args.env_file)
    if not env_path.exists():
        raise RuntimeError(f"Env file not found: {env_path}")

    env = load_env_file(env_path)
    allow_empty_keys = args.allow_empty_keys or env.get("DEPLOY_SKIP_KEYS") == "1"
    if args.cleanup_only:
        require_keys(env, REQUIRED_CLEANUP_KEYS)
    elif allow_empty_keys:
        require_keys(env, REQUIRED_CLEANUP_KEYS)
    else:
        require_keys(env, REQUIRED_ENV_KEYS)

    host = env["DEPLOY_HOST"]
    user = env["DEPLOY_USER"]
    password = env["DEPLOY_PASS"]
    port = int(env.get("DEPLOY_PORT", "22"))
    deploy_dir = env.get("DEPLOY_DIR", DEFAULT_DEPLOY_DIR).rstrip("/")
    cleanup_paths = parse_cleanup_paths(env.get("DEPLOY_CLEANUP_PATHS", ""))
    config_rel = env.get("DEPLOY_CONFIG", "user_data/config_ccxt_main.json").lstrip("/")
    config_list = parse_cleanup_paths(env.get("DEPLOY_CONFIGS", ""))
    if not config_list:
        config_list = [config_rel]
    config_paths = [f"{deploy_dir}/{item.lstrip('/')}" for item in config_list]
    monitor_port = int(env.get("MONITOR_PORT", "9010"))
    monitor_symbol = env.get("MONITOR_SYMBOL", "BTC")
    monitor_exchange = env.get("MONITOR_EXCHANGE", "binance")
    monitor_timeframe = env.get("MONITOR_TIMEFRAME", "5m")
    monitor_quote = env.get("MONITOR_QUOTE", "USDT")
    monitor_limit = env.get("MONITOR_LIMIT", "120")
    monitor_momentum_bars = env.get("MONITOR_MOMENTUM_BARS", "12")
    monitor_volatility_bars = env.get("MONITOR_VOLATILITY_BARS", "14")
    monitor_ema_fast = env.get("MONITOR_EMA_FAST", "9")
    monitor_ema_slow = env.get("MONITOR_EMA_SLOW", "21")
    monitor_cache_ttl = env.get("MONITOR_CACHE_TTL", "10")
    monitor_sandbox = env.get("MONITOR_SANDBOX", "")
    monitor_default_type = env.get("MONITOR_DEFAULT_TYPE", "")
    monitor_price_type = env.get("MONITOR_PRICE_TYPE", "")
    monitor_api_key = env.get("MONITOR_API_KEY", "")
    monitor_api_secret = env.get("MONITOR_API_SECRET", "")
    monitor_api_password = env.get("MONITOR_API_PASSWORD", "")
    monitor_state_files = env.get(
        "MONITOR_STATE_FILES",
        "user_data/ai_iteration_state_main.json,user_data/ai_iteration_state_alt.json",
    )
    monitor_feedback_files = env.get(
        "MONITOR_FEEDBACK_FILES",
        "user_data/ai_iteration_feedback_main.jsonl,user_data/ai_iteration_feedback_alt.jsonl",
    )
    monitor_whale_report = env.get("MONITOR_WHALE_FLOW_REPORT", "")
    monitor_retail_report = env.get("MONITOR_RETAIL_FOMO_REPORT", "")
    cornna_domain = env.get("CORNNA_DOMAIN", "cornna.dpdns.org")
    cornna_web_root = env.get("CORNNA_WEB_ROOT", "/var/www/cornna")
    anomaly_enable = env.get("ANOMALY_ENABLE", "0") == "1"
    anomaly_config = env.get("ANOMALY_CONFIG", "user_data/anomaly/anomaly_config.json")
    anomaly_config_target = (
        anomaly_config
        if anomaly_config.startswith("/")
        else f"{deploy_dir}/{anomaly_config.lstrip('/')}"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tar_path = Path(tmpdir) / "freqtrade_payload.tar.gz"
        if not args.cleanup_only:
            build_payload_tarball(tar_path)

        client = connect_ssh(host, user, password, port)
        sftp = client.open_sftp()

        try:
            if args.cleanup or args.cleanup_only:
                if not cleanup_paths:
                    raise RuntimeError("Cleanup requested but DEPLOY_CLEANUP_PATHS is empty.")
                remove_remote_paths(client, deploy_dir, cleanup_paths)
                if args.cleanup_only:
                    print("Cleanup completed.")
                    return

            if args.cleanup_ai:
                ai_paths = discover_ai_iteration_paths(client, deploy_dir)
                if ai_paths:
                    remove_remote_paths(client, deploy_dir, ai_paths)

            install_system_packages(client)

            run_remote(client, f"mkdir -p {deploy_dir}")
            run_remote(
                client,
                f"if [ ! -d {deploy_dir}/.git ]; then "
                f"git clone https://github.com/freqtrade/freqtrade.git {deploy_dir}; "
                f"else git -C {deploy_dir} pull --ff-only; fi",
            )

            run_remote(
                client,
                f"python3 -m venv {deploy_dir}/.venv",
            )
            run_remote(
                client,
                f"{deploy_dir}/.venv/bin/pip install --upgrade pip wheel setuptools",
            )
            run_remote(
                client,
                f"{deploy_dir}/.venv/bin/pip install -r {deploy_dir}/requirements.txt",
            )
            run_remote(
                client,
                f"{deploy_dir}/.venv/bin/pip install -e {deploy_dir}",
            )

            remote_tar = run_remote_capture(
                client,
                "mktemp /tmp/freqtrade_payload.XXXXXX.tar.gz",
            ).strip()
            sftp.put(str(tar_path), remote_tar)
            run_remote(client, f"tar -xzf {remote_tar} -C {deploy_dir}")
            run_remote(client, f"rm -f {remote_tar}")

            env_content = (
                f"FREQTRADE__EXCHANGE__KEY={env.get('FREQTRADE__EXCHANGE__KEY', '')}\n"
                f"FREQTRADE__EXCHANGE__SECRET={env.get('FREQTRADE__EXCHANGE__SECRET', '')}\n"
            )
            write_remote_file(sftp, f"{deploy_dir}/.env", env_content, mode=0o600)

            if anomaly_enable:
                config_dir = anomaly_config_target.rsplit("/", maxsplit=1)[0]
                run_remote(client, f"mkdir -p {shlex.quote(config_dir)}")
                run_remote(
                    client,
                    "if [ ! -f {target} ]; then cp {source} {target}; fi".format(
                        target=shlex.quote(anomaly_config_target),
                        source=shlex.quote(f"{deploy_dir}/config_examples/anomaly_monitor.json"),
                    ),
                )

            for config_path in config_paths:
                service_name = service_name_from_config(config_path)
                service_content = "\n".join(
                    [
                        "[Unit]",
                        f"Description=Freqtrade {service_name}",
                        "After=network-online.target",
                        "Wants=network-online.target",
                        "",
                        "[Service]",
                        "Type=simple",
                        f"WorkingDirectory={deploy_dir}",
                        f"EnvironmentFile={deploy_dir}/.env",
                        f"ExecStart={deploy_dir}/.venv/bin/python -m freqtrade trade "
                        f"-c {config_path}",
                        "Restart=on-failure",
                        "RestartSec=5",
                        "",
                        "[Install]",
                        "WantedBy=multi-user.target",
                        "",
                    ]
                )
                write_remote_file(
                    sftp,
                    f"/etc/systemd/system/{service_name}.service",
                    service_content,
                    mode=0o644,
                )

            run_remote(client, "systemctl daemon-reload")
            for config_path in config_paths:
                service_name = service_name_from_config(config_path)
                run_remote(client, f"systemctl enable --now {service_name}")

            if not args.no_monitor:
                monitor_exec = (
                    f"{deploy_dir}/.venv/bin/python "
                    f"{deploy_dir}/scripts/ccxt_monitor_api.py"
                )
                monitor_env = [
                    f"Environment=MONITOR_PORT={monitor_port}",
                    f"Environment=MONITOR_SYMBOL={monitor_symbol}",
                    f"Environment=MONITOR_EXCHANGE={monitor_exchange}",
                    f"Environment=MONITOR_TIMEFRAME={monitor_timeframe}",
                    f"Environment=MONITOR_QUOTE={monitor_quote}",
                    f"Environment=MONITOR_LIMIT={monitor_limit}",
                    f"Environment=MONITOR_MOMENTUM_BARS={monitor_momentum_bars}",
                    f"Environment=MONITOR_VOLATILITY_BARS={monitor_volatility_bars}",
                    f"Environment=MONITOR_EMA_FAST={monitor_ema_fast}",
                    f"Environment=MONITOR_EMA_SLOW={monitor_ema_slow}",
                    f"Environment=MONITOR_CACHE_TTL={monitor_cache_ttl}",
                    "Environment=MONITOR_REFRESH=15",
                    f"Environment=MONITOR_STATE_FILES={monitor_state_files}",
                    f"Environment=MONITOR_FEEDBACK_FILES={monitor_feedback_files}",
                ]
                if monitor_sandbox:
                    monitor_env.append(f"Environment=MONITOR_SANDBOX={monitor_sandbox}")
                if monitor_default_type:
                    monitor_env.append(
                        f"Environment=MONITOR_DEFAULT_TYPE={monitor_default_type}"
                    )
                if monitor_price_type:
                    monitor_env.append(f"Environment=MONITOR_PRICE_TYPE={monitor_price_type}")
                if monitor_api_key:
                    monitor_env.append(f"Environment=MONITOR_API_KEY={monitor_api_key}")
                if monitor_api_secret:
                    monitor_env.append(
                        f"Environment=MONITOR_API_SECRET={monitor_api_secret}"
                    )
                if monitor_api_password:
                    monitor_env.append(
                        f"Environment=MONITOR_API_PASSWORD={monitor_api_password}"
                    )
                if monitor_whale_report:
                    monitor_env.append(
                        f"Environment=MONITOR_WHALE_FLOW_REPORT={monitor_whale_report}"
                    )
                if monitor_retail_report:
                    monitor_env.append(
                        f"Environment=MONITOR_RETAIL_FOMO_REPORT={monitor_retail_report}"
                    )
                monitor_service = "\n".join(
                    [
                        "[Unit]",
                        "Description=Cornna AI Monitor API",
                        "After=network-online.target",
                        "Wants=network-online.target",
                        "",
                        "[Service]",
                        "Type=simple",
                        f"WorkingDirectory={deploy_dir}",
                        f"ExecStart={monitor_exec}",
                        *monitor_env,
                        "Restart=on-failure",
                        "RestartSec=5",
                        "",
                        "[Install]",
                        "WantedBy=multi-user.target",
                        "",
                    ]
                )
                write_remote_file(
                    sftp,
                    "/etc/systemd/system/cornna-monitor.service",
                    monitor_service,
                    mode=0o644,
                )
                run_remote(client, "systemctl daemon-reload")
                run_remote(client, "systemctl enable --now cornna-monitor")

                nginx_conf = render_cornna_nginx(cornna_domain, cornna_web_root, monitor_port)
                write_remote_file(
                    sftp,
                    f"/etc/nginx/sites-available/{cornna_domain}.conf",
                    nginx_conf,
                    mode=0o644,
                )
                run_remote(client, "nginx -t")
                run_remote(client, "systemctl reload nginx")

            if anomaly_enable:
                anomaly_exec = (
                    f"{deploy_dir}/.venv/bin/python "
                    f"{deploy_dir}/scripts/anomaly_monitor.py --config {anomaly_config}"
                )
                anomaly_service = "\n".join(
                    [
                        "[Unit]",
                        "Description=Cornna Anomaly Monitor",
                        "After=network-online.target",
                        "Wants=network-online.target",
                        "",
                        "[Service]",
                        "Type=simple",
                        f"WorkingDirectory={deploy_dir}",
                        f"ExecStart={anomaly_exec}",
                        "Restart=on-failure",
                        "RestartSec=5",
                        "",
                        "[Install]",
                        "WantedBy=multi-user.target",
                        "",
                    ]
                )
                write_remote_file(
                    sftp,
                    "/etc/systemd/system/cornna-anomaly.service",
                    anomaly_service,
                    mode=0o644,
                )
                run_remote(client, "systemctl daemon-reload")
                run_remote(client, "systemctl enable --now cornna-anomaly")
        finally:
            sftp.close()
            client.close()

    print("Deployment completed. Check status with: systemctl status freqtrade-* --no-pager")


if __name__ == "__main__":
    main()

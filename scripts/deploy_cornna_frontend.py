#!/usr/bin/env python3
"""
Deploy Cornna frontend to a VPS and configure Nginx + acme.sh SSL (Cloudflare DNS-01).
"""
from __future__ import annotations

import argparse
import os
import posixpath
import sys
from pathlib import Path

import paramiko


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy Cornna frontend to VPS.")
    parser.add_argument("--host", required=True, help="VPS host/IP")
    parser.add_argument("--user", default="root", help="SSH user")
    parser.add_argument("--password", required=True, help="SSH password")
    parser.add_argument("--domain", default="cornna.dpdns.org", help="Domain name")
    parser.add_argument(
        "--local-dir",
        default=str(Path("web") / "cornna"),
        help="Local frontend directory",
    )
    parser.add_argument(
        "--remote-dir",
        default="/var/www/cornna",
        help="Remote web root directory",
    )
    parser.add_argument(
        "--cf-token",
        default=os.getenv("CF_Token", ""),
        help="Cloudflare API token for DNS-01",
    )
    parser.add_argument(
        "--account-email",
        default=os.getenv("ACME_EMAIL", ""),
        help="ACME account email (recommended)",
    )
    return parser.parse_args()


def connect_ssh(host: str, user: str, password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # noqa: S507
    client.connect(hostname=host, username=user, password=password, timeout=20)
    return client


def run_cmd(client: paramiko.SSHClient, command: str) -> tuple[int, str, str]:
    _stdin, stdout, stderr = client.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="ignore")
    err = stderr.read().decode("utf-8", errors="ignore")
    return exit_status, out, err


def ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    parts = remote_dir.strip("/").split("/")
    path = ""
    for part in parts:
        path = f"{path}/{part}"
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)


def upload_directory(sftp: paramiko.SFTPClient, local_dir: Path, remote_dir: str) -> None:
    ensure_remote_dir(sftp, remote_dir)
    for root, _, files in os.walk(local_dir):
        rel = Path(root).relative_to(local_dir)
        target_dir = posixpath.join(remote_dir, str(rel).replace("\\", "/"))
        ensure_remote_dir(sftp, target_dir)
        for filename in files:
            local_path = Path(root) / filename
            remote_path = posixpath.join(target_dir, filename)
            sftp.put(str(local_path), remote_path)


def render_nginx_config(domain: str, web_root: str, ssl_enabled: bool) -> str:
    if ssl_enabled:
        return f"""
server {{
  listen 80;
  server_name {domain};
  return 301 https://$host$request_uri;
}}

server {{
  listen 443 ssl http2;
  server_name {domain};
  root {web_root};
  index index.html;

  ssl_certificate /etc/ssl/certs/{domain}.cer;
  ssl_certificate_key /etc/ssl/private/{domain}.key;

  add_header X-Frame-Options "SAMEORIGIN" always;
  add_header X-Content-Type-Options "nosniff" always;
  add_header Referrer-Policy "strict-origin-when-cross-origin" always;

  location / {{
    try_files $uri $uri/ =404;
  }}
}}
""".strip()
    return f"""
server {{
  listen 80;
  server_name {domain};
  root {web_root};
  index index.html;

  location / {{
    try_files $uri $uri/ =404;
  }}
}}
""".strip()


def write_remote_file(sftp: paramiko.SFTPClient, path: str, content: str) -> None:
    with sftp.file(path, "w") as handle:
        handle.write(content)


def main() -> int:
    args = parse_args()
    local_dir = Path(args.local_dir).resolve()
    if not local_dir.exists():
        print(f"Local directory not found: {local_dir}", file=sys.stderr)
        return 1

    if not args.cf_token:
        print("Missing Cloudflare token: provide --cf-token or CF_Token env var.", file=sys.stderr)
        return 1

    client = connect_ssh(args.host, args.user, args.password)
    sftp = client.open_sftp()

    try:
        run_cmd(client, "mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled")
        run_cmd(client, f"mkdir -p {args.remote_dir}")

        upload_directory(sftp, local_dir, args.remote_dir)

        config_path = f"/etc/nginx/sites-available/{args.domain}.conf"
        config_payload = render_nginx_config(args.domain, args.remote_dir, False)
        write_remote_file(sftp, config_path, config_payload)
        run_cmd(client, f"ln -sf {config_path} /etc/nginx/sites-enabled/{args.domain}.conf")

        install_cmd = (
            "if command -v apt-get >/dev/null 2>&1; then "
            "apt-get update -y && apt-get install -y nginx curl; "
            "elif command -v dnf >/dev/null 2>&1; then "
            "dnf install -y nginx curl; "
            "elif command -v yum >/dev/null 2>&1; then "
            "yum install -y nginx curl; "
            "fi"
        )
        run_cmd(client, install_cmd)
        run_cmd(client, "systemctl enable --now nginx || true")
        run_cmd(client, "nginx -t && systemctl reload nginx")

        acme_home = "$HOME/.acme.sh"
        acme_email = args.account_email or f"admin@{args.domain}"
        install_acme = (
            f"if [ ! -f {acme_home}/acme.sh ]; then "
            f"curl https://get.acme.sh | sh -s email={acme_email}; fi"
        )
        run_cmd(client, install_acme)

        issue_cmd = (
            f"export CF_Token='{args.cf_token}'; "
            f"{acme_home}/acme.sh --issue --dns dns_cf -d {args.domain} --server letsencrypt"
        )
        status, out, err = run_cmd(client, issue_cmd)
        if status != 0:
            skip_markers = ("Skipping. Next renewal time", "Domains not changed.")
            if not any(marker in out for marker in skip_markers):
                print(out)
                print(err, file=sys.stderr)
                return 2

        install_cert_cmd = (
            f"{acme_home}/acme.sh --install-cert -d {args.domain} "
            f"--key-file /etc/ssl/private/{args.domain}.key "
            f"--fullchain-file /etc/ssl/certs/{args.domain}.cer "
            f"--reloadcmd \"systemctl reload nginx\""
        )
        run_cmd(client, "mkdir -p /etc/ssl/private /etc/ssl/certs")
        status, out, err = run_cmd(client, install_cert_cmd)
        if status != 0:
            combined = f"{out}\n{err}"
            if "nginx.service is not active" in combined:
                run_cmd(client, "systemctl enable --now nginx || systemctl start nginx || true")
                run_cmd(client, "nginx -t && systemctl reload nginx")
            else:
                print(out)
                print(err, file=sys.stderr)
                return 3

        config_payload = render_nginx_config(args.domain, args.remote_dir, True)
        write_remote_file(sftp, config_path, config_payload)
        run_cmd(client, "nginx -t && systemctl reload nginx")

        print("Deployment complete.")
        return 0
    finally:
        sftp.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())

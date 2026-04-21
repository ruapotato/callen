# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Site management — GitHub Pages repos + Cloudflare DNS.

Each managed site is:
  - A GitHub repo under the configured org/user
  - A CNAME subdomain on freesoft.page pointing at GitHub Pages
  - Tracked in Callen's SQLite DB so the agent knows what exists
"""

import json
import logging
import subprocess
import urllib.request
import urllib.error
import urllib.parse

log = logging.getLogger(__name__)


class SiteManager:
    def __init__(self, config):
        self.domain = config.domain
        self.zone_id = config.cloudflare_zone_id
        self.cf_token = config.cloudflare_api_token
        self.github_org = config.github_org
        self.template_repo = config.template_repo

    # --- Cloudflare DNS ---

    def _cf_api(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"https://api.cloudflare.com/client/v4/zones/{self.zone_id}/{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={
                "Authorization": f"Bearer {self.cf_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            log.error("Cloudflare API %s %s -> %d: %s", method, path, e.code, body_text[:500])
            raise

    def add_subdomain(self, subdomain: str) -> dict:
        """Create a CNAME record: subdomain.freesoft.page -> github_org.github.io"""
        target = f"{self.github_org}.github.io"
        try:
            result = self._cf_api("POST", "dns_records", {
                "type": "CNAME",
                "name": subdomain,
                "content": target,
                "proxied": True,
                "ttl": 1,  # auto
            })
            log.info("DNS: %s.%s -> %s (id=%s)",
                     subdomain, self.domain, target,
                     result.get("result", {}).get("id", "?"))
            return result.get("result", {})
        except urllib.error.HTTPError as e:
            if e.code == 400:
                # CNAME likely already exists — find and return it
                existing = self._cf_api("GET",
                    f"dns_records?type=CNAME&name={subdomain}.{self.domain}")
                for rec in existing.get("result", []):
                    log.info("DNS: %s.%s already exists (id=%s)",
                             subdomain, self.domain, rec["id"])
                    return rec
            raise

    def remove_subdomain(self, subdomain: str) -> bool:
        """Delete the CNAME record for a subdomain."""
        records = self._cf_api("GET",
            f"dns_records?type=CNAME&name={subdomain}.{self.domain}")
        for rec in records.get("result", []):
            self._cf_api("DELETE", f"dns_records/{rec['id']}")
            log.info("DNS: deleted %s.%s (id=%s)", subdomain, self.domain, rec["id"])
            return True
        return False

    def list_subdomains(self) -> list[dict]:
        """List all CNAME records on the zone."""
        result = self._cf_api("GET", "dns_records?type=CNAME&per_page=100")
        return [
            {"name": r["name"], "content": r["content"], "id": r["id"]}
            for r in result.get("result", [])
        ]

    # --- GitHub repos ---

    def _gh(self, *args, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=60,
        )
        if check and result.returncode != 0:
            log.error("gh %s failed: %s", " ".join(args), result.stderr[:500])
            raise RuntimeError(f"gh failed: {result.stderr[:200]}")
        return result

    def create_repo(self, name: str, from_template: str | None = None) -> str:
        """Create a GitHub repo. Returns the repo URL."""
        template = from_template or self.template_repo
        repo_full = f"{self.github_org}/{name}"

        # Check if template exists; if not, create a bare repo
        if template:
            try:
                self._gh("repo", "view", template)
                self._gh(
                    "repo", "create", repo_full,
                    "--template", template,
                    "--public", "--clone=false",
                )
                log.info("Repo created from template: %s", repo_full)
                return f"https://github.com/{repo_full}"
            except RuntimeError:
                log.warning("Template %s not found, creating bare repo", template)

        try:
            self._gh(
                "repo", "create", repo_full,
                "--public", "--add-readme",
            )
            log.info("Bare repo created: %s", repo_full)
        except RuntimeError:
            if self.repo_exists(name):
                log.info("Repo %s already exists, reusing", repo_full)
            else:
                raise
        return f"https://github.com/{repo_full}"

    def delete_repo(self, name: str) -> bool:
        """Delete a GitHub repo. Use with caution."""
        repo_full = f"{self.github_org}/{name}"
        try:
            self._gh("repo", "delete", repo_full, "--yes")
            log.info("Repo deleted: %s", repo_full)
            return True
        except RuntimeError:
            return False

    def enable_pages(self, name: str, subdomain: str) -> None:
        """Enable GitHub Pages on the repo and set the custom domain.

        GitHub requires at least one commit on main before Pages can be
        enabled. We push CNAME + a minimal index.html first, then flip
        the Pages switch.
        """
        repo_full = f"{self.github_org}/{name}"
        fqdn = f"{subdomain}.{self.domain}"

        # Push CNAME file (also serves as the initial commit if repo is empty)
        self._upsert_file(repo_full, "CNAME", fqdn + "\n",
                          f"Set custom domain {fqdn}")

        # Push a minimal index.html so the site has something visible
        index_html = (
            f"<!DOCTYPE html>\n<html><head><title>{subdomain}.{self.domain}"
            f"</title></head>\n<body>\n<h1>{subdomain}.{self.domain}</h1>\n"
            f"<p>Site coming soon.</p>\n</body></html>\n"
        )
        self._upsert_file(repo_full, "index.html", index_html,
                          "Initial placeholder page")

        # Now enable Pages — main branch should have commits
        try:
            self._gh(
                "api", f"repos/{repo_full}/pages",
                "--method", "POST",
                "--field", "source[branch]=main",
                "--field", "source[path]=/",
            )
        except RuntimeError:
            # Pages might already be enabled
            pass

        # Set custom domain
        try:
            self._gh(
                "api", f"repos/{repo_full}/pages",
                "--method", "PUT",
                "--field", f"cname={fqdn}",
            )
        except RuntimeError:
            log.warning("Failed to set custom domain on %s — may need manual config", repo_full)

        log.info("Pages enabled on %s with domain %s", repo_full, fqdn)

    @staticmethod
    def _b64(text: str) -> str:
        import base64
        return base64.b64encode(text.encode()).decode()

    def _upsert_file_binary(self, repo_full: str, path: str, content_bytes: bytes, message: str):
        """Create or update a binary file in a GitHub repo."""
        import base64
        sha_args = []
        try:
            result = self._gh(
                "api", f"repos/{repo_full}/contents/{path}",
                "--jq", ".sha",
            )
            sha = result.stdout.strip()
            if sha:
                sha_args = ["--field", f"sha={sha}"]
        except RuntimeError:
            pass

        b64_content = base64.b64encode(content_bytes).decode()
        try:
            self._gh(
                "api", f"repos/{repo_full}/contents/{path}",
                "--method", "PUT",
                "--field", f"message={message}",
                "--field", f"content={b64_content}",
                *sha_args,
            )
        except RuntimeError:
            log.warning("Failed to upsert binary %s in %s", path, repo_full)

    def _upsert_file(self, repo_full: str, path: str, content: str, message: str):
        """Create or update a file in a GitHub repo (handles SHA for updates)."""
        # Check if file already exists to get its SHA
        sha_args = []
        try:
            result = self._gh(
                "api", f"repos/{repo_full}/contents/{path}",
                "--jq", ".sha",
            )
            sha = result.stdout.strip()
            if sha:
                sha_args = ["--field", f"sha={sha}"]
        except RuntimeError:
            pass  # file doesn't exist yet, that's fine

        try:
            self._gh(
                "api", f"repos/{repo_full}/contents/{path}",
                "--method", "PUT",
                "--field", f"message={message}",
                "--field", f"content={self._b64(content)}",
                *sha_args,
            )
        except RuntimeError:
            log.warning("Failed to upsert %s in %s", path, repo_full)

    def list_repos(self) -> list[dict]:
        """List repos in the org/user."""
        result = self._gh(
            "repo", "list", self.github_org,
            "--json", "name,url,description,updatedAt",
            "--limit", "200",
        )
        return json.loads(result.stdout) if result.stdout.strip() else []

    def repo_exists(self, name: str) -> bool:
        result = self._gh(
            "repo", "view", f"{self.github_org}/{name}",
            check=False,
        )
        return result.returncode == 0

    # --- High-level: create a full site ---

    def create_site(self, subdomain: str, template: str | None = None) -> dict:
        """Full site creation: repo + DNS + Pages config.

        Returns a summary dict with URLs and status.
        """
        repo_name = subdomain  # repo name = subdomain for simplicity
        fqdn = f"{subdomain}.{self.domain}"

        # 1. Create the repo
        repo_url = self.create_repo(repo_name, from_template=template)

        # 2. Add DNS CNAME
        dns_record = self.add_subdomain(subdomain)

        # 3. Enable GitHub Pages with custom domain
        self.enable_pages(repo_name, subdomain)

        return {
            "subdomain": subdomain,
            "fqdn": fqdn,
            "url": f"https://{fqdn}",
            "repo": repo_url,
            "dns_record_id": dns_record.get("id"),
            "status": "created",
        }

    def delete_site(self, subdomain: str) -> dict:
        """Tear down a site: delete repo + DNS record."""
        repo_deleted = self.delete_repo(subdomain)
        dns_deleted = self.remove_subdomain(subdomain)
        return {
            "subdomain": subdomain,
            "repo_deleted": repo_deleted,
            "dns_deleted": dns_deleted,
        }

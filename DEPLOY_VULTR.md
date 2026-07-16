# Deploying HealthBoard to Vultr

This runs the FastAPI app on a Vultr VPS with Docker, behind Nginx with HTTPS.
Your **database (Neon)** and **file storage (Cloudflare R2)** stay where they are —
Vultr only runs the app, so there's nothing to migrate.

---

## 0. What you need
- A Vultr account
- Your `.env` values ready: Neon `DATABASE_URL`, R2 `S3_*`, a strong `JWT_SECRET`,
  and (optional) `CEIPAL_*`, `SENDGRID_API_KEY`
- A domain you can point at the server (e.g. `board.yourcompany.com`) — optional
  but needed for HTTPS

---

## 1. Create the server
Vultr → **Deploy New Server**:
- **Cloud Compute – Shared CPU**
- **Ubuntu 24.04 LTS**
- Size: **2 GB RAM / 1 vCPU** is plenty for an internal team (≈$12/mo). Go 4 GB if
  you'll import millions of résumés on the same box.
- Add your SSH key.

Note the server's **IP address**.

**DNS (for HTTPS):** at your domain registrar add an **A record**:
`board.yourcompany.com → <server IP>`.

---

## 2. Install Docker
SSH in (`ssh root@<server IP>`) and run:
```bash
apt update && apt -y upgrade
curl -fsSL https://get.docker.com | sh
```

---

## 3. Get the code onto the server
Either clone from your Git host:
```bash
cd /opt
git clone <your-repo-url> healthboard
cd healthboard
```
…or, if the code isn't in Git, from your Windows machine (PowerShell) copy it up:
```powershell
scp -r C:\Users\vs510\PycharmProjects\Halo root@<server IP>:/opt/healthboard
```
(Do **not** copy `.venv`, `uploads`, or `__pycache__`.)

---

## 4. Create the production `.env`
On the server, in `/opt/healthboard`:
```bash
cp .env.production.example .env
nano .env
```
Fill in (these are the ones that matter):
```
ENVIRONMENT=production
DEBUG=false
DATABASE_URL=postgresql://neondb_owner:...@...neon.tech/neondb?sslmode=require
JWT_SECRET=<paste a fresh 48+ char random string>
CORS_ORIGINS=https://board.yourcompany.com
FRONTEND_BASE_URL=https://board.yourcompany.com

STORAGE_ENABLED=true
S3_ENDPOINT_URL=https://<acct>.r2.cloudflarestorage.com
S3_BUCKET=jobboard
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_ACL=

# optional
CEIPAL_ENABLED=true
CEIPAL_EMAIL=...
CEIPAL_PASSWORD=...
CEIPAL_API_KEY=...
CEIPAL_REPORT_URL=...
ADMIN_EMAIL=you@yourcompany.com
ADMIN_PASSWORD=<strong password>   # creates your admin on first boot
```
Generate a JWT secret with:
`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`

> Security: `.env` holds live secrets — it's already git-ignored. Never commit it.

---

## 5. Build and run the app
```bash
docker build -t healthboard .
docker run -d --name healthboard --restart unless-stopped \
  --env-file .env -p 127.0.0.1:8000:8000 healthboard
```
Check it's up:
```bash
docker logs -f healthboard          # Ctrl-C to stop tailing
curl -s localhost:8000/api/health   # -> {"status":"ok",...}
```
Binding to `127.0.0.1:8000` keeps the app private — Nginx (next) is the only thing
exposed to the internet.

---

## 6. One-time: create the performance indexes
These make the Providers directory fast at millions of rows (run once; safe to re-run):
```bash
docker exec -it healthboard python -m app.migrate_provider_indexes
# if this is a brand-new database (not your current Neon), also run:
docker exec -it healthboard python -m app.migrate_provider_fields
```

---

## 7. Nginx + HTTPS
```bash
apt -y install nginx
cat >/etc/nginx/sites-available/healthboard <<'NGINX'
server {
    listen 80;
    server_name board.yourcompany.com;
    client_max_body_size 20M;              # résumé uploads
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
NGINX
ln -s /etc/nginx/sites-available/healthboard /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```
Add a free HTTPS certificate:
```bash
apt -y install certbot python3-certbot-nginx
certbot --nginx -d board.yourcompany.com
```
Certbot auto-renews. Your board is now live at **https://board.yourcompany.com**.

---

## 8. Restrict to your team (internal use)
Because it's internal, lock the firewall to your office/VPN IPs:
```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw enable
```
For tighter control, restrict port 443 to specific IPs, or put it behind
Cloudflare Access / a VPN. Everyone still needs to log in regardless.

---

## Updating after a code change
```bash
cd /opt/healthboard
git pull                       # or scp the changed files up
docker build -t healthboard .
docker rm -f healthboard
docker run -d --name healthboard --restart unless-stopped \
  --env-file .env -p 127.0.0.1:8000:8000 healthboard
```

## Handy commands
| Task | Command |
|---|---|
| Logs | `docker logs -f healthboard` |
| Restart | `docker restart healthboard` |
| Shell in container | `docker exec -it healthboard bash` |
| Sync Ceipal jobs | `docker exec -it healthboard python -m app.importers.ceipal_jobs` |
| Import résumés | `docker exec -it healthboard python -m app.importers.resumes "/path/in/container"` |

## Notes
- **Neon cold starts:** on Neon's free tier the DB "sleeps" when idle, so the first
  request after a quiet period takes ~10s. For a live internal team, disable
  autosuspend on the Neon compute (or use a paid tier) so it's always warm.
- **Workers:** `WEB_CONCURRENCY` (default 3) sets gunicorn workers. Rule of thumb:
  `2 × vCPU + 1`. Set it in `.env` if you resize the server.
- **Résumé imports** at multi-million scale are memory/CPU heavy — run those on a
  bigger box or a separate worker, not during peak hours.

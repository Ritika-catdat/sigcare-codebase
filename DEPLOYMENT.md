# Sigcare – Running and Deploying (Docker)

This repo ships the **Document Quality API** (`sigcare_quality.api`) as a single
Docker container. The same `docker compose` setup runs on a laptop and on the
staging EC2. Infra details (account, VM, bucket, logins) live in the
git-ignored `documents/` folder and must never be committed.

## 1. What is in the container

| Item | Value |
|---|---|
| Base image | `python:3.11-slim` |
| System packages | `tesseract-ocr` (page-counter OCR fallback), `libglib2.0-0` (OpenCV), `curl` (healthcheck) |
| App | `uvicorn sigcare_quality.api:app` on port `8000` |
| Health endpoint | `GET /health` → `{"status":"ok","service":"Sigcare Document Quality API"}` |
| Job outputs | `/app/api_outputs` (docker volume `sigcare_outputs`) |
| Runs as | non-root user `sigcare` |

Endpoints: `GET /health`, `POST /api/v1/quality-check` (PDF upload),
`GET /api/v1/quality-check/{job_id}`, `.../{job_id}/artifacts`,
`.../{job_id}/artifact/{path}`. Interactive docs at `/docs`.

## 2. Run locally

```bash
cp .env.example .env          # edit if needed; defaults work locally
docker compose build
docker compose up -d
curl http://localhost:8000/health
curl -F "file=@sample.pdf" "http://localhost:8000/api/v1/quality-check?dpi=220"
docker compose logs -f api    # logs
docker compose down           # stop (add -v to also delete job outputs)
```

## 3. Staging deployment (Dev_VM, AWS London)

Target from `documents/Setup_Details.docx`: one Ubuntu EC2 (`t3.xlarge`,
100 GB EBS) in `eu-west-2`, Docker already installed, reached through
**AWS Systems Manager Session Manager** (no SSH key). Bucket
`sigcare-test-bucket` exists for test files.

### 3.1 One-time server preparation

1. Sign in to the AWS console with the IAM user from `documents/User_Login_Details.docx`
   (change that password on first login and enable MFA).
2. EC2 → Instances → `Dev_VM` → **Connect** → **Session Manager** → Connect.
3. In the session:
   ```bash
   sudo su - ubuntu
   docker --version && docker compose version     # both must work
   sudo usermod -aG docker ubuntu && newgrp docker  # only if "permission denied"
   sudo apt-get update && sudo apt-get install -y git
   ```
4. Give the VM read access to GitHub. Easiest: a **deploy key**.
   ```bash
   ssh-keygen -t ed25519 -C "sigcare-dev-vm" -f ~/.ssh/sigcare_deploy -N ""
   cat ~/.ssh/sigcare_deploy.pub
   ```
   Add the public key in GitHub → repo → Settings → Deploy keys (read-only).
   Then:
   ```bash
   cat >> ~/.ssh/config <<'CFG'
   Host github.com
     IdentityFile ~/.ssh/sigcare_deploy
     IdentitiesOnly yes
   CFG
   git clone git@github.com:Ritika-catdat/sigcare-codebase.git ~/sigcare
   ```
5. Create the server `.env` (never copy it back out, never commit it):
   ```bash
   cd ~/sigcare
   cp .env.example .env
   nano .env        # APP_ENV=staging, AWS_REGION=eu-west-2, S3_BUCKET_NAME=sigcare-test-bucket
   chmod 600 .env
   ```
6. Open the port. In the EC2 **security group** allow inbound TCP `8000`
   **only from the office/VPN IP** (not `0.0.0.0/0`). Prescription data must
   not be reachable from the open internet. Later put nginx/ALB with HTTPS
   (port 443) in front and close 8000.

### 3.2 Deploy / redeploy (every release)

```bash
cd ~/sigcare
git pull origin main            # or the branch being tested
docker compose build
docker compose up -d
docker compose ps               # STATUS should show "healthy" after ~20s
curl -s http://localhost:8000/health
```

From your laptop (after the security-group rule):

```bash
curl http://18.170.97.204:8000/health
```

Rollback = `git checkout <previous-commit> && docker compose up -d --build`.

### 3.3 Smoke test on the server

```bash
# copy a test PDF from S3 (use anonymised data only)
aws s3 cp s3://sigcare-test-bucket/samples/test.pdf /tmp/test.pdf
curl -F "file=@/tmp/test.pdf" http://localhost:8000/api/v1/quality-check | head -c 600
```
`aws` CLI needs the instance IAM role to allow `s3:GetObject` on the bucket
(instance role, not personal keys – see infra docs).

### 3.4 Useful operations

```bash
docker compose logs -f --tail=200 api     # live logs
docker compose restart api
docker compose down && docker compose up -d
docker system prune -f                    # reclaim disk from old images
docker volume ls | grep sigcare           # job outputs volume
```

## 4. Environment variables

See `.env.example`. Only `SIGCARE_*` and `APP_ENV` are used by the code today.
The AWS / DB / ConX / Microsoft 365 entries are placeholders taken from the
infrastructure documents so the staging `.env` is ready as the pipeline grows.
On EC2 use the **instance IAM role** for AWS access; do not put access keys in `.env`.

## 5. What is deliberately not done yet

- No HTTPS / reverse proxy, no authentication on the API. Keep port 8000
  restricted by security group until nginx + TLS (or ALB) is added.
- No CI/CD. The infra docs propose GitHub Actions → ECR → deploy; for now
  deployment is manual (`git pull` + `docker compose up -d --build`).
- No `/ready` endpoint (docs mention `/health` and `/ready`). `/health` exists.
- Job outputs are kept forever in the `sigcare_outputs` volume; add a cleanup
  policy before real data is used.

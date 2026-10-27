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

Endpoints: `GET /health`, `GET /ready` (AWS credentials, Textract staging
bucket, Tesseract), `POST /api/v1/quality-check` (PDF upload),
`GET /api/v1/quality-check/{job_id}`, `.../{job_id}/artifacts`,
`.../{job_id}/artifact/{path}`. Interactive docs at `/docs`.

Processing runs in the background. `POST` returns **202** at once with a
`job_id`. Poll `GET /api/v1/quality-check/{job_id}`: while it runs you get
`{"status": "RUNNING", "stage": "FRONTSHEET|SPLITTING|QUALITY|TEXTRACT", "progress": "n/total"}`.
When finished you get the full result (`COMPLETED`, `COMPLETED_WITH_REVIEW`)
or `FAILED` with an `error`. A large PDF takes minutes, so never wait on the POST.

Pipeline per job: Sigma front sheets are detected and **removed**, the remaining
pages are split into prescriptions, each prescription gets a quality check and
cleaned images, then AWS Textract (async, via the staging bucket) produces
`processed/<prescription>/prescription.json` in the downstream contract from
`prescription_ocr/output_contract.py` (flat patient fields, `in_possession`,
`not_in_possession`, `allergy`, handwritten notes, `flag_for_review`,
`review_reasons` (only filled when `flag_for_review` is true), `prescription_number`,
`original_pdf_location`, `prescription_location`). Textract block/word ids and
bounding boxes are not included. The same JSON is under `results[].prescription`
in the job result.

### S3 worker (service `worker`)

`docker compose up -d` also starts `sigcare-worker`. It runs
`python -m sigcare_quality.s3_watcher`, processes files itself (it does not call
the API), and checks the day's input folder every `SIGCARE_WATCH_INTERVAL_SECONDS`
(default 60). The bucket is set per environment with `SIGCARE_S3_BUCKET` (test
and prod differ). "Today" uses `SIGCARE_S3_TIMEZONE` (default Europe/London);
yesterday is also checked (`SIGCARE_WATCH_LOOKBACK_DAYS=1`). The older names
`S3_BUCKET_NAME`, `S3_POLL_INTERVAL_SECONDS` and `S3_WORKER_DRY_RUN` still work.

```
<yyyy>/<MMM>/<dd>/input/<file>.pdf                         drop new files here
<yyyy>/<MMM>/<dd>/intermediary/<file>/original/<file>.pdf  unchanged copy, saved before cleaning
<yyyy>/<MMM>/<dd>/intermediary/<file>/split_prescriptions/ split PDFs + split_report.txt
<yyyy>/<MMM>/<dd>/intermediary/<file>/cleanup/<rx>/        cleaned page images
<yyyy>/<MMM>/<dd>/intermediary/<file>/forensics/<rx>/      quality reports
<yyyy>/<MMM>/<dd>/intermediary/<file>/extraction/<rx>/     prescription.json (downstream contract)
<yyyy>/<MMM>/<dd>/intermediary/<file>/reports/             front-sheet report, frontsheets_removed.pdf
<yyyy>/<MMM>/<dd>/intermediary/<file>/manifest/            status.json, result.json, job_summary.json
<yyyy>/<MMM>/<dd>/processed/<file>.pdf                     input moved here when done
<yyyy>/<MMM>/<dd>/processed/<file>.json                    final structured result
```

Folder format is `yyyy/MMM/dd`, e.g. `2026/Oct/27/input/Document_261012.pdf`
(three-letter month, first letter capital; `SIGCARE_S3_MONTH_CASE=title`). Set
`SIGCARE_S3_ROOT_PREFIX` if the date folders sit under a top-level folder.

Rules:
- The input moves to `processed/` only after every output is stored.
- If `processed/<file>` already exists, the input is a duplicate and is deleted.
- If `manifest/job_summary.json` exists but the input is still in `input/`, the
  file is skipped for manual recovery.
- A failed file stays in `input/` with `manifest/status.json` = `FAILED` and the
  error, and is skipped. Delete that `status.json` to retry it.
- `SIGCARE_WATCH_DRY_RUN=true` only logs eligible files.

Useful commands:
```bash
docker compose logs -f worker
docker compose run --rm worker python -m sigcare_quality.s3_watcher --once   # single scan
```

## 2. Run locally

```bash
cp .env.example .env          # edit if needed; defaults work locally
docker compose build
docker compose up -d
curl http://localhost:8000/health
curl http://localhost:8000/ready
curl -F "file=@sample.pdf" "http://localhost:8000/api/v1/quality-check?dpi=220"
# -> {"job_id": "<id>", "status": "QUEUED", "status_url": "/api/v1/quality-check/<id>"}
curl http://localhost:8000/api/v1/quality-check/<id>      # repeat until COMPLETED / FAILED
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
curl -s http://localhost:8000/ready            # every check must be "ok": true
curl -F "file=@/tmp/test.pdf" http://localhost:8000/api/v1/quality-check
curl -s http://localhost:8000/api/v1/quality-check/<job_id> | head -c 600
docker compose logs -f api                      # per-stage progress and timings
```

### 3.4 AWS access for Textract

The container uses the EC2 **instance IAM role**. It needs, for the staging
bucket in `SIGCARE_TEXTRACT_STAGING_BUCKET` (same region as `AWS_REGION`):

- `textract:StartDocumentAnalysis`, `textract:GetDocumentAnalysis`
- `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` on `arn:aws:s3:::<bucket>/sigcare-temporary/*`
- `s3:ListBucket` on `arn:aws:s3:::<bucket>` (used by the `/ready` check)

For the S3 worker, the role also needs, on `SIGCARE_S3_BUCKET`:
`s3:ListBucket` on the bucket and `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`
on `arn:aws:s3:::<bucket>/*` (it reads `input/`, writes `intermediary/` and
`processed/`, and deletes the input after moving it). If
`SIGCARE_TEXTRACT_STAGING_BUCKET` is empty, Textract staging uses
`SIGCARE_S3_BUCKET` under `sigcare-temporary/`.

**Docker on EC2 and IMDSv2:** with the default hop limit of 1, a container
cannot reach the instance metadata service, so it gets **no credentials**.
Every Textract call then fails (`NoCredentialsError` in `/ready`). Fix once per instance:

```bash
aws ec2 modify-instance-metadata-options --instance-id <instance-id>     --http-tokens required --http-put-response-hop-limit 2
```

Add a lifecycle rule on the staging bucket that expires `sigcare-temporary/`
after 1 day as a safeguard for objects that failed to delete.
`aws` CLI needs the instance IAM role to allow `s3:GetObject` on the bucket
(instance role, not personal keys – see infra docs).

### 3.5 Useful operations

```bash
docker compose logs -f --tail=200 api     # live logs
docker compose restart api
docker compose down && docker compose up -d
docker system prune -f                    # reclaim disk from old images
docker volume ls | grep sigcare           # job outputs volume
```

## 4. Environment variables

See `.env.example`. The code reads `SIGCARE_*`, `AWS_REGION`, `POLL_SECONDS`
and `TIMEOUT_SECONDS`.
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

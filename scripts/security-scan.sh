#!/usr/bin/env bash
# Security gates: the one implementation used by CI (.github/workflows/ci.yml) and for local reproduction.
#
#   scripts/security-scan.sh all       # every stage below; reports each failure, exits non-zero if any failed
#   scripts/security-scan.sh deps      # pip-audit (every locked Python package, hash-verified), npm audit,
#                                      #   npm registry signatures
#   scripts/security-scan.sh sbom      # CycloneDX SBOMs: Python runtime + all groups, npm runtime
#   scripts/security-scan.sh secrets   # gitleaks over the full git history (default rules + .gitleaks.toml)
#   scripts/security-scan.sh sast      # semgrep (Python, TypeScript/React, OWASP Top 10, Dockerfile, Actions,
#                                      #   secrets) + bandit
#   scripts/security-scan.sh images    # Trivy on nuvexa-backend:$IMAGE_TAG and nuvexa-frontend:$IMAGE_TAG
#                                      #   (built when missing): vulnerabilities + secrets, image SBOMs
#   scripts/security-scan.sh config    # Trivy misconfiguration scan, actionlint, zizmor, exception register
#
# Gates and policy: docs/CI_SECURITY_GATES.md. Accepted exceptions: security-exceptions.toml.
# Needs Docker, uv, npm and git. Scanner images are pinned by digest; update them deliberately, after the
# 7-day release cooldown. Reports go to $SECURITY_REPORTS (default reports/security/).
set -euo pipefail
cd "$(dirname "$0")/.."

GITLEAKS_IMAGE="ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
SEMGREP_IMAGE="semgrep/semgrep:1.181.0@sha256:81474464c1f07483ba0682a44787fd93c91397419762a04abe2bafaae396304b"
TRIVY_IMAGE="aquasec/trivy:0.76.0@sha256:2416d1f74045b57274df1a340eee6b510cacd73c66a6ccb8da092b117eb7d222"
ACTIONLINT_IMAGE="rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667"
ZIZMOR_IMAGE="ghcr.io/zizmorcore/zizmor:1.31.1@sha256:42d409dbb851d4c5d35a058cf042b828bea1e3f2884378c526eb0974780e18d9"

OUT="${SECURITY_REPORTS:-reports/security}"
IMAGE_TAG="${IMAGE_TAG:-ci}"
mkdir -p "$OUT"
OUT_ABS="$(cd "$OUT" && pwd)"

summary() { if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then printf '%s\n' "$@" >> "$GITHUB_STEP_SUMMARY"; fi; }

# Scanners run from their pinned images with the repository mounted read-only.
gitleaks() {
  docker run --rm --quiet -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=/repo \
    -v "$PWD:/repo:ro" -v "$OUT_ABS:/out" -w /repo "$GITLEAKS_IMAGE" "$@"
}
semgrep() { docker run --rm --quiet -v "$PWD:/src:ro" -v "$OUT_ABS:/out" -w /src "$SEMGREP_IMAGE" semgrep "$@"; }
trivy() {
  docker run --rm --quiet -v "$PWD:/src:ro" -v "$OUT_ABS:/out" -v "$SCAN_DIR:/scan:ro" \
    -v nuvexa-trivy-cache:/root/.cache/trivy -e TRIVY_SKIP_VERSION_CHECK=true -w /src "$TRIVY_IMAGE" "$@"
}
actionlint() { docker run --rm --quiet -v "$PWD:/repo:ro" -w /repo "$ACTIONLINT_IMAGE" "$@"; }
zizmor() { docker run --rm --quiet -e GH_TOKEN -v "$PWD:/repo:ro" -w /repo "$ZIZMOR_IMAGE" "$@"; }
security_tools() { uv run --isolated --locked --only-group security "$@"; }

stage_deps() {
  echo "== pip-audit: every locked Python package (all dependency groups), hashes required"
  uv export --locked --all-groups --no-emit-project --format requirements.txt --quiet -o "$OUT/requirements-audit.txt"
  security_tools pip-audit --strict --disable-pip --require-hashes --progress-spinner off --desc on \
    -r "$OUT/requirements-audit.txt" | tee "$OUT/pip-audit.txt"
  echo "== npm audit: fails on high or critical advisories; every advisory is listed"
  (cd frontend && npm audit --audit-level=high) | tee "$OUT/npm-audit.txt"
  echo "== npm audit signatures: registry signatures and provenance of the installed packages"
  if [ ! -d frontend/node_modules ]; then (cd frontend && npm ci --no-audit --no-fund); fi
  (cd frontend && npm audit signatures) | tee "$OUT/npm-signatures.txt"
  summary "### Dependency audit" "- pip-audit: $(tail -n 1 "$OUT/pip-audit.txt")" \
    "- npm audit: $(grep -E 'vulnerabilit' "$OUT/npm-audit.txt" | tail -n 1)"
}

stage_sbom() {
  echo "== CycloneDX SBOMs"
  uv export --locked --no-dev --no-emit-project --format cyclonedx1.5 --quiet -o "$OUT/sbom-backend-runtime.cdx.json"
  uv export --locked --all-groups --no-emit-project --format cyclonedx1.5 --quiet -o "$OUT/sbom-backend-all.cdx.json"
  (cd frontend && npm sbom --sbom-format cyclonedx --omit dev --package-lock-only) > "$OUT/sbom-frontend-runtime.cdx.json"
  ls -l "$OUT"/sbom-*.cdx.json
}

stage_secrets() {
  echo "== gitleaks: every commit (default rules + precise allowlists in .gitleaks.toml)"
  gitleaks git --config /repo/.gitleaks.toml --redact --no-banner --no-color --exit-code 1 \
    --report-format sarif --report-path /out/gitleaks.sarif . 2>&1 | tee "$OUT/gitleaks.log"
  # A scan that silently saw no history must not pass (e.g. a shallow clone or a git ownership error).
  local scanned expected
  scanned="$(grep -oE '[0-9]+ commits scanned' "$OUT/gitleaks.log" | grep -oE '^[0-9]+' || echo 0)"
  expected="$(git rev-list --count HEAD)"
  echo "gitleaks scanned ${scanned} commit(s); HEAD has ${expected}"
  [ "$scanned" -ge "$expected" ]
  summary "### Secret scan" "- gitleaks: no leaks in ${scanned} commits"
}

stage_sast() {
  echo "== semgrep"
  semgrep scan --metrics=off --disable-version-check --error \
    --config p/python --config p/owasp-top-ten --config p/react --config p/typescript \
    --config p/dockerfile --config p/github-actions --config p/secrets \
    --sarif-output=/out/semgrep.sarif --text 2>&1 | tee "$OUT/semgrep.txt"
  echo "== bandit (all severities and confidence levels)"
  security_tools bandit -r app scripts | tee "$OUT/bandit.txt"
  summary "### SAST" "- semgrep: $(grep -E 'Findings:' "$OUT/semgrep.txt" | tail -n 1 | xargs)" \
    "- bandit: $(grep -E 'No issues identified|Total issues' "$OUT/bandit.txt" | head -n 1 | xargs)"
}

trivy_counts() {
  python3 - "$1" <<'PY'
import collections, json, sys

report = json.load(open(sys.argv[1]))
counts = collections.Counter()
for result in report.get("Results") or []:
    for vuln in result.get("Vulnerabilities") or []:
        counts[(vuln["Severity"], "fixable" if vuln.get("FixedVersion") else "no fix")] += 1
    counts[("SECRET", "found")] += len(result.get("Secrets") or [])
cells = ", ".join(f"{sev} {kind}: {n}" for (sev, kind), n in sorted(counts.items()) if n) or "none"
print(cells)
PY
}

stage_images() {
  SCAN_DIR="$(mktemp -d)"
  trap 'rm -rf "$SCAN_DIR"' RETURN
  summary "### Container images (Trivy)"
  local image ref context
  for image in backend frontend; do
    ref="nuvexa-$image:$IMAGE_TAG"
    context=.
    if [ "$image" = frontend ]; then context=frontend; fi
    if ! docker image inspect "$ref" >/dev/null 2>&1; then docker build -t "$ref" "$context"; fi
    docker save "$ref" -o "$SCAN_DIR/$image.tar"
    echo "== $ref: full report (every severity, fixable or not)"
    trivy image --input "/scan/$image.tar" --scanners vuln,secret --no-progress --format json \
      --output "/out/trivy-$image.json"
    trivy image --input "/scan/$image.tar" --scanners vuln,secret --no-progress --format cyclonedx \
      --output "/out/sbom-$image-image.cdx.json"
    summary "- $ref — all findings: $(trivy_counts "$OUT/trivy-$image.json")"
    echo "== $ref: gate (HIGH/CRITICAL with an available fix, or any secret)"
    # SEC-EX-001: findings without a published fix are listed by the report above, not gated here.
    trivy image --ignore-unfixed --exit-code 1 --severity HIGH,CRITICAL --scanners vuln,secret --no-progress \
      --input "/scan/$image.tar" | tee "$OUT/trivy-$image-gate.txt"
  done
}

stage_config() {
  SCAN_DIR="$(mktemp -d)"
  trap 'rm -rf "$SCAN_DIR"' RETURN
  echo "== Trivy misconfiguration scan (Dockerfiles, Compose, Kubernetes/IaC, secrets in config)"
  trivy config --exit-code 1 --severity LOW,MEDIUM,HIGH,CRITICAL . | tee "$OUT/trivy-config.txt"
  echo "== actionlint (workflow syntax, expressions, shellcheck on run scripts)"
  actionlint -color
  echo "== zizmor (GitHub Actions security: injection, permissions, pinning, credentials)"
  if [ -n "${GH_TOKEN:-}" ]; then zizmor .github; else zizmor --offline .github; fi
  echo "== scanner exception register"
  git ls-files -z | python3 scripts/check_security_exceptions.py
  summary "### Pipeline configuration" "- Trivy config, actionlint, zizmor: clean; exception register valid"
}

stages=(deps sbom secrets sast images config)
case "${1:-}" in
  all)
    failed=()
    for stage in "${stages[@]}"; do "$0" "$stage" || failed+=("$stage"); done
    if [ "${#failed[@]}" -gt 0 ]; then echo "FAILED stages: ${failed[*]}" >&2; exit 1; fi
    echo "All security stages passed."
    ;;
  deps | sbom | secrets | sast | images | config) "stage_$1" ;;
  *)
    echo "usage: $0 {all|${stages[*]// /|}}" >&2
    exit 2
    ;;
esac

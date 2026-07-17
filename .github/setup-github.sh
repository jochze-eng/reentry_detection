#!/usr/bin/env bash
# One-time setup of the structured GitHub workflow for this repo.
# Creates the canonical label set and (optionally) a Projects board.
#
# Prerequisites:
#   1. Install the GitHub CLI:  https://cli.github.com/   (winget install GitHub.cli)
#   2. Authenticate:            gh auth login
#   3. Run from the repo root:  bash .github/setup-github.sh
#
# Safe to re-run: label creation uses --force to update existing labels.
set -euo pipefail

REPO="jochze-eng/reentry_detection"
log() { printf '\033[1;34m[setup]\033[0m %s\n' "$1"; }

command -v gh >/dev/null 2>&1 || { echo "gh CLI not found. Install from https://cli.github.com/ and run 'gh auth login'." >&2; exit 1; }

log "Creating / updating labels on ${REPO}..."

create_label() { gh label create "$1" --repo "$REPO" --color "$2" --description "$3" --force; }

# Type
create_label "type: bug"          "d73a4a" "Something is broken or behaving incorrectly."
create_label "type: feature"      "0e8a16" "New capability or enhancement."
create_label "type: chore"        "cfd3d7" "Refactor, build, deps, or maintenance."
create_label "type: docs"         "0075ca" "Documentation only."
# Priority
create_label "priority: critical" "b60205" "Production down / data loss / security."
create_label "priority: high"     "d93f0b" "Important; schedule for the current cycle."
create_label "priority: medium"   "fbca04" "Normal priority."
create_label "priority: low"      "c2e0c6" "Nice to have; no time pressure."
# Status
create_label "status: triage"     "ededed" "Newly filed; needs review and prioritization."
create_label "status: in-progress" "1d76db" "Actively being worked on."
create_label "status: blocked"    "e99695" "Waiting on a dependency or decision."
create_label "status: in-review"  "5319e7" "PR open and awaiting review."
# Area
create_label "area: LPR"          "bfdadc" "License Plate Recognition monitor."
create_label "area: FR"           "bfdadc" "Face Recognition monitor."
create_label "area: auth"         "bfdadc" "Authentication, users, RBAC."
create_label "area: infra"        "bfdadc" "Deploy, Docker, installer, database."
create_label "area: ui"           "bfdadc" "Web dashboards and static frontend."

log "Labels done."

# --- Optional: create a Projects (v2) board ---
# Requires the 'project' scope:  gh auth refresh -s project,read:project
read -r -p "Create a GitHub Projects board 'Recurring Target Detection'? [y/N] " ans
if [[ "${ans:-N}" =~ ^[Yy]$ ]]; then
  OWNER="jochze-eng"
  log "Creating project board..."
  gh project create --owner "$OWNER" --title "Recurring Target Detection" \
    && log "Board created. Open it from https://github.com/users/${OWNER}/projects to add the Status column values: Backlog, In Progress, In Review, Done." \
    || echo "Project creation failed — you may need: gh auth refresh -s project,read:project" >&2
else
  log "Skipped project board. You can create one later in the GitHub UI (Projects tab → New project → Board)."
fi

log "Setup complete."

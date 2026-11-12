---
name: skill-manager
description: Use the skm CLI to manage project agent skills and curate shared skill catalogs. Use when adding sources, maintaining tags or reusable profiles, selecting project skills, installing or updating managed skills, diagnosing installation drift, or uninstalling manager-owned skills. Covers catalog YAML, .skill-manager.yaml, and .skill-manager.lock.
---

# Skill Manager

Skill Manager lets you curate skill sources and reusable profiles centrally,
while each project explicitly chooses its selection and when to adopt updates.
A catalog defines available sources and profiles; a project declaration selects
from that catalog, and its lock records the catalog snapshot and Git revisions.
Independent local skill directories remain live. Installation exposes selected
skills through managed links in the project.

## Read the relevant workflow

- **Project selection, install, update, drift, or uninstall:** read
  [Manage a project](references/project-workflow.md).
- **Catalog sources, approved refs, discovery rules, tags, or shared profiles:**
  read [Maintain a catalog](references/catalog-workflow.md).
- For a task spanning both, read the catalog workflow first and the project
  workflow when adopting the changes. Load only the references the task needs.

Use the explicit project installer CLI (0.2 or later). Check `skm --help` and
subcommand help for available options; use `uv run skm` when working from the
Skill Manager source checkout. Edit declarations and catalogs, and let the CLI
manage locks, links, and installation bookkeeping. Keep changes within the
requested project or catalog scope.

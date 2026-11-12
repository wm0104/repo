# Repository maintenance

This workspace maintains the APT source at https://wm0104.github.io/repo/.
It builds a static Pages/APT artifact, not an installable package.

## Build and checks

On a host with Python 3, dpkg-deb, dpkg-scanpackages and zstd:

```sh
python3 -m unittest discover -s testsuite -v
python3 scripts/build_repo.py --output _site
```

The build validates all debs, copies `depictions/` into `_site/`, and generates
Packages, compressed indexes and Release. `_site/` is ignored; never commit
those generated files. The current iPhone environment lacks Python 3,
dpkg-scanpackages and zstd, so the complete build/tests must run on a suitable
host or in CI. Native depiction JSON and text can be checked independently;
that is not a Sileo rendering test.

`.github/workflows/refresh.yml` audits/repairs package permissions, builds the
site, commits verified permission repairs, and deploys Pages on a
push to main (or workflow_dispatch). Do not invoke the repair scripts locally
for a depiction-only change: existing deb bytes must remain unchanged.

## KeyBridge depiction

Stable package fields for `com.wm0104.keybridge`:

- Depiction: https://wm0104.github.io/repo/depictions/com.wm0104.keybridge/index.html
- SileoDepiction: https://wm0104.github.io/repo/depictions/com.wm0104.keybridge/depiction.json
- Section: Ai插件

The index generator supports explicit Section mappings in `upstreams.json`
(configured sources and the automatically saved `manual_sections` map)
and native depictions from opted-in changelog fields. Other fields still come
from each deb, so adding pages alone does not attach them to old
versions. The existing 0.0.1/0.0.2 debs have no depiction fields and keep their
original Section. Future packages should include the above fields in their
own build metadata. Connecting existing index records without changing debs
requires a separately approved override limited to this package (and tests
preserving all archive hashes). Never edit generated Packages by hand.

Keep changelogs from 0.0.2 onward, with no dates and no invented 0.0.1 entry.
Each change starts with a literal ASCII hyphen and space: use native
DepictionLabelView text and HTML paragraphs, not Markdown lists.

Do not change README or existing debs for this task. Do not push or publish
without explicit authorization. After an authorized push, check the workflow
and both stable URLs, refresh the source in Sileo and check package details
and the changelog tab; HTML fallback also needs a browser rendering check.

## Unified upstream management

Only `.github/workflows/sync-upstream.yml` is a sync entry ("同步插件").
It is manual-only; do not create a workflow per repository or package. Keep
`.github/workflows/refresh.yml` as the only Pages publisher. The obsolete
single-package workflow has been removed; GitHub may retain its historical runs.

The Run workflow form has operation, scope, source_type, repo, packages and
section. Keep the three Chinese usage comments directly above the repo,
packages and section inputs; do not require comments on every YAML line.
Pass inputs through environment variables, never shell interpolation.
The default operation is read-only list. Operations:
- List: show saved repositories, names, Package IDs, Sections, and stopped entries in
  the run summary. Do not download, commit, or dispatch publication.
- Add and sync: provide one source and comma-separated names/codes/Package IDs sharing a
  Section. Save each successful registration automatically to `sources`.
  Repeating the same registration is idempotent; differing ownership/Section
  requires explicit modify, not silent replacement.
- Update: all saved entries, one saved source, or selected saved Package IDs.
  Use the actual debs in this checkout, never Sileo/device state or online
  indexes as a substitute for this repository's own versions.
- Modify: explicitly move registered Package IDs to another source or Section;
  validate the new upstream selection before saving each successful change.
- Stop: resolve saved names/codes/Package IDs, remove their sync registrations but retain
  all debs and save their Sections in `manual_sections`; never delete packages.

`upstreams.json.sources` supports multiple sources. Original entries without
`type` mean github; APT entries use `type: apt` and an HTTPS directory in `repo`.
Each actual Package ID has one owner, including case-sensitive legacy IDs.
Keep existing mappings and unknown top-level metadata unchanged unless the
selected operation requires changing that exact entry. Fila is not initially
registered. `manual_sections` preserves classification without scheduling sync.

"查找插件" is read-only and accepts source-local name keywords (blank lists all
matching-architecture candidates). APT discovery reads only the index. GitHub
discovery temporarily downloads and verifies assets to read control Name;
do not infer Name from filenames. Cache those temporary downloads within the
same run for add/modify, then clean them. Neither search nor list publishes.
Complete names may resolve only uniquely; partial words only suggest choices.
Duplicate names require a candidate code, never selecting the first result.
Codes bind source type/address, real Package, Version and Architecture, not
result order; expired/cross-source codes fail closed. Missing Name falls back
to Package for display with the same code-selection path. Keep exact Package
input compatibility and store `names` only as display metadata, never identity.
Updates/stops resolve registered names from local debs/cache without contacting
upstreams just to identify a selection. Updating all continues by Package even
if the author changes the display name. Summary shows names, versions,
architectures and confirmation codes; source addresses remain configurable.

GitHub sources read the latest formal Release, parse real deb metadata, and
require asset SHA256 and size. APT sources support public HTTPS flat indexes
Packages.xz/bz2/gz/plain, with bounded downloads/decompression. Pick the highest
Debian version for selected Package IDs and `iphoneos-arm64e`; require SHA256,
Size and a safe relative Filename, then verify actual downloaded Package,
Version and Architecture against the index. APT hashes come from the HTTPS
index; this is not GPG signature authentication. Do not send GH_TOKEN to APT.
No account-bound/paid download APIs, automatic architecture conversion, or
suite/component repositories are implied to work.

Higher versions are added without deleting old packages. Equal versions are
skipped; different bytes produce a visible message while preserving the old
archive. Lower versions are skipped. Isolate individual missing/corrupt/bad
packages and unreachable sources so other validated updates can proceed;
failed new registrations are not saved. Record failure rows and make the run
fail after successfully committing/dispatching the valid portion. Never report
partial success as all-success. Ambiguous choices are not guessed.

Validate each candidate with the existing audit/build on a temporary one-package
checkout, then gate the combined configuration and packages through the full
repository audit/build. Any required archive permission repair is rejected,
not silently applied to source debs. Global preflight failure or stale main
prevents the whole commit. Config promotion failure rolls back new debs.
Commit only new validated deb paths and the exact prevalidated config bytes;
reject tracked deb replacements, unrelated staged files and config drift.
Use Chinese commit messages, stale-main guards and non-force push.

GITHUB_TOKEN pushes do not trigger on:push. Explicitly dispatch refresh.yml
following a successful import/config change, or successful no-change update
for retry recovery. A sync result means publication was requested, not finished.
Keep the upstream-sync lock separate from pages; do not wait for Pages while
holding its lock. No real import without user authorization. Use fixture debs,
controlled HTTP responses and local bare Git remotes for tests; distinguish
those from a live source-index read, real import, Pages deployment and Sileo QA.

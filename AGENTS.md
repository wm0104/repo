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

The manual sync entries share the same engine and `upstream-sync` lock:
- `.github/workflows/add-plugin.yml` (添加插件): choose GitHub/APT, provide a source,
  optional name keyword/PackageID/candidate code, and a Section when adding.
  Blank selection only lists candidates; leave Section blank too. A unique keyword
  match may be added; multiple matches list real candidate codes and stop.
- `.github/workflows/sync-upstream.yml` (更新插件): one generated choice selects all
  active registrations or one package. No scope, source or manual Package fields.
- `.github/workflows/manage-sync.yml` (管理同步): view, modify or stop. Modify/stop
  selects one active registration. View leaves selection/source/Section blank.
  Modify requires a new source and/or Section; blank values retain existing ones,
  including source type. A replacement source must contain the same real PackageID.
  Stop preserves all debs and the Section in `manual_sections`.
Keep `.github/workflows/refresh.yml` as the only Pages publisher. Do not create a
workflow per repository/package. Inputs go through environment variables, never
shell interpolation; reject conflicting/unrelated fields rather than ignoring them.

GitHub addresses accept `owner/repo`, `https://github.com/owner/repo`, a `/releases`
list or `/releases/tag/version` URL, normalized to `owner/repo`. Version links do
not pin a release: the run explicitly notes that it still reads the latest formal
release. Addition keeps an explicit source_type choice so public HTTPS APT sources
are not confused with GitHub links. Management infers a supplied replacement from
these explicit address forms: owner/repo or github.com links for GitHub, other
public HTTPS directory addresses for APT. A blank replacement preserves the type.

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
Unique complete names and unique partial keywords can add; ambiguous selections
require a candidate code, never selecting the first result.
Codes bind source type/address, real Package, Version and Architecture, not
result order; expired/cross-source codes fail closed. Missing Name falls back
to Package for display with the same code-selection path. Keep exact Package
input compatibility and store `names` only as display metadata, never identity.
Updates/stops resolve registered names from local debs/cache without contacting
upstreams just to identify a selection. Updating all continues by Package even
if the author changes the display name. Summary shows names, versions,
architectures and confirmation codes; source addresses remain configurable.

`saved_package` is generated in both update and management workflows, displaying
Name plus real PackageID. Update has an all-registered option; management has a
no-selection placeholder for read-only view. Validate IDs against current active
registrations so stale forms cannot revive stopped entries. Internal scripts keep
legacy comma-separated selection compatibility; it is not exposed by these forms.
`saved_packages.py` changes only the marked options blocks of those two exact
workflow paths; never grant a generic workflow-file write allowance. Recompute the
exact allowed bytes of every affected workflow from the captured Git baseline and
current validated registration before push; reject omissions and non-choice edits.
`manage_saved.py prepare` downloads/audits/builds without the dedicated write token,
then records exact file hashes and expected bytes in RUNNER_TEMP. The separate
publish step receives WORKFLOW_WRITE_TOKEN (fine-grained PAT, target repository
only, Contents and Workflows write). No token in Git URLs/config/logs; checkout
uses persist-credentials:false. All three new forms fail before mutation/download
when a write operation lacks WORKFLOW_WRITE_TOKEN; read-only list/search need no
dedicated token and do not update choices or publish.

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
for retry recovery. A successful dedicated-PAT push triggers on:push itself;
skip explicit dispatch only in that case to avoid duplicate publication.
A sync result means publication was requested, not finished.
Keep the upstream-sync lock separate from pages; do not wait for Pages while
holding its lock. No real import without user authorization. Use fixture debs,
controlled HTTP responses and local bare Git remotes for tests; distinguish
those from a live source-index read, real import, Pages deployment and Sileo QA.

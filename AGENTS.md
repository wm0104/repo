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

## Manual upstream synchronization

`.github/workflows/sync-upstream.yml` has only `workflow_dispatch`, with no
scheduled runs. On main, run "同步插件" to check all configured GitHub
repositories' latest formal releases. `upstreams.json` maps exact Package IDs
to this source's Sections; it is not limited to Apelin or 乌龙. Fila is not in
the initial configuration. Add another source entry for another repository.

The comparison is against the actual debs checked out from this repository,
not Sileo, the installed device, the published Packages file, filenames, or a
shared release tag. Actual Architecture must be iphoneos-arm64e. GitHub asset
SHA256 digests are required; absent digests fail closed. Keep original bytes.

Higher versions are added without deleting old packages. Equal version and
equal bytes are skipped; equal version with different bytes is reported and
skipped, preserving the repository's original package without blocking higher
versions of other packages. Lower versions are skipped. Missing/ambiguous
assets, bad digests, unsafe paths and validation failures must not partially
import packages.

Before import, a temporary full candidate checkout runs the existing audit
and build. Any required permission repair aborts sync; do not silently rewrite
upstream packages. Batch sync commits only new validated deb paths. Single
sync may also save only the selected Package's `manual_sections` entry, after
the candidate config and packages pass the same audit/build. Both use Chinese
messages, stale-main guards and non-force push. The original refresh workflow
remains the only Pages publisher and must not be replaced or bypassed.

GITHUB_TOKEN pushes do not trigger on:push. The sync explicitly dispatches
refresh.yml, even on no-change runs so a previous dispatch failure can recover.
Sync success means publication was requested, not that Pages has finished;
verify the separate Publish APT repository run. Never place both workflows
under the pages lock while waiting for one from the other.

Run unit tests using local fixture debs and bare Git remotes before deployment.
Do not run real upstream synchronization without user authorization.

### Single-package form

"同步单个上游插件" (`.github/workflows/sync-single-upstream.yml`) is a separate
manual-only entry. Fill `repo` with owner/repository, `package` with the actual
Package ID, and `section` with the desired Section. Inputs are passed through
environment variables, never interpolated into shell commands. It reuses
`sync_upstream.py --single` and the `upstream-sync` lock, so it does not run
alongside batch sync. It processes only the current selection, not all sources.

The workflow automatically saves the selected Section in `manual_sections`;
these entries are not extra batch sources. Existing configured packages may
be selected individually but must retain their configured Section. Missing
assets, validation or promotion failures must not save a partial config or
package update. Equal-version packages remain untouched even when saving a
new Section. Future builds retain the saved mapping without repacking debs.

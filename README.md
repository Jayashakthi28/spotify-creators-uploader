# spotify-creators-uploader

Publish one video podcast episode at a time to **Spotify for Creators** from the command line,
headlessly, using a saved Playwright session created after a one-time human login. The same code is
a typed Python library: it fills in the title, description, explicit-content setting and optional
thumbnail, then publishes immediately — or walks the whole wizard without clicking Publish when you
ask for a dry run.

> **Unofficial — use at your own risk.**
> This project is not affiliated with, endorsed by, or supported by Spotify. Spotify offers no
> public upload API, so this tool automates the Spotify for Creators web UI instead. It **may break
> at any time** when Spotify changes the site, and automated access may conflict with Spotify's
> terms or guidelines. Use it only with accounts you can afford to put at risk. The tool never uses
> stealth or evasion; if Spotify challenges the login, sign in again interactively.

## Install

Requires Python 3.11 or newer.

```bash
pip install spotify-creators-uploader
# or, in an isolated environment:
pipx install spotify-creators-uploader

# Playwright's browser is a separate download:
spotify-uploader install-browser
```

The console script is `spotify-uploader`; `python -m spotify_uploader` runs the same CLI.

## First-time login

Sign in once on a machine with a screen:

```bash
spotify-uploader login
spotify-uploader status   # offline: session present? show id? ledger size?
spotify-uploader check    # online and headless: is the saved session valid?
```

`login` defaults to `--mode auto`: it starts your real Google Chrome with a dedicated profile
(`~/.spotify-uploader/chrome-profile`) and attaches over CDP, because Google rejects sign-in inside
a Playwright-launched browser. Chrome 136+ requires that separate profile for remote debugging. Use
`--mode playwright` only on a host without Chrome, and `--show-id SHOW_ID` if the post-login page is
a show chooser with no id in the URL. `--timeout SECONDS` defaults to 600.

`import-session` accepts a Playwright `storage_state.json` or a browser-exported cookie list:

```bash
spotify-uploader import-session ~/Downloads/cookies.json
```

## Several shows

One login covers every show on the account, so **always say which show you mean**. Spotify has no
page that lists all of an account's shows: opening the dashboard without an id silently lands on the
*last show you used*. The show id is in the dashboard URL, `creators.spotify.com/home/show/<ID>`.

```bash
spotify-uploader shows                      # the shows this tool can find, with name and setup state
spotify-uploader shows --select ID_OR_NAME  # make one the default for later commands
spotify-uploader upload v.mp4 --title T --description D --show-id OTHER_ID   # one-off choice
```

`shows` is best effort: it reads the show you last used from Spotify, any other shows its dashboard
links to, and every show id this tool already knows locally (your default and your upload history).
If Spotify's dashboard does not list your other shows, paste the id from that show's dashboard URL.
`--select` matches an id or a case-insensitive name.

`login` saves the first show id it sees, so confirm it: a wrong id uploads to the wrong show without
any error. Use `--home DIR` to keep a second Spotify account's session and ledger separate. Before
uploading, the uploader reads the show's name and setup state, prints which show it is publishing to,
and refuses a show whose setup is unfinished. The upload history is kept per show, so the same video
can be published to two shows.

## Prerequisites Spotify enforces

Automation cannot and should not do these for you:

- **Verify the email address** on the account (the episodes page says so until you do).
- **Finish the show's own setup** — name, description and cover art, the dashboard's "Set up your
  show" item. Until then Publish opens a *"Finish setting up to publish"* dialog and keeps the
  episode as a draft. The uploader detects this, stops in seconds and raises `ShowSetupError` naming
  the draft; after setup, retry with `--resume-draft EPISODE_ID` instead of uploading the video
  again.

## Prepare the video

Spotify's video specs want one video and one audio track, no MP4 edit lists, stereo AAC at 128 kbps
or more, and Rec.709 colour. Videos from many editors miss some of those (edit-list atoms, mono
audio, a BT.601 colour matrix), so check before you upload. Needs `ffmpeg` and `ffprobe` on `PATH`.

```bash
spotify-uploader prepare --check video.mp4        # report problems; exit 2 if any
spotify-uploader prepare video.mp4 ready.mp4      # re-encode, then verify the result
```

`prepare` converts the colour matrix (BT.601 to BT.709) instead of only relabelling it, because
relabelling would visibly shift colours. It writes `ready.mp4` only after the output passes the same
check, so a failed encode leaves nothing behind. Upload the prepared file, not the original.

## Upload

```bash
spotify-uploader upload video.mp4 \
  --title "Paper title" \
  --description-file description.txt \
  --show-id SHOW_ID
```

`--description "..."` replaces the file. Add `--explicit` for explicit content, `--thumbnail IMAGE`
for a custom thumbnail (horizontal 16:9, 1920x1080 suggested, PNG or JPEG), `--dry-run` to walk the
whole wizard without publishing, and `--resume-draft EPISODE_ID` to reopen an existing draft instead
of uploading the video again.

`--dry-run` still leaves a draft on Spotify: **every upload creates a draft**, including a dry run
and any run that fails after the upload. `--dry-run` is never recorded in the local ledger. Related
flags: `--force` republishes a file whose SHA-256 is already in the ledger, `--headed` shows the
browser, `--channel chrome` runs the system Chrome, `--debug-dir DIR` saves a screenshot, page HTML
and trace for a failed step, and `--upload-timeout SECONDS` raises the video-upload limit. Progress
lines go to stderr; the published episode is one JSON object on stdout.

## Doctor

```bash
spotify-uploader doctor [--offline] [--rehearse] [--video PATH] [--thumbnail IMAGE] [--json]
```

`doctor` diagnoses the install, the saved session and the selected show. `--offline` skips every
network check. With `--rehearse`, it also walks the whole upload wizard as a dry run to find Spotify
UI changes before a real publish; that still leaves a draft on Spotify. `--json` prints JSON.

## Python API

```python
import asyncio
from pathlib import Path

from spotify_uploader import EpisodeRequest, SpotifyCreatorsUploader, UploaderConfig


async def main() -> None:
    config = UploaderConfig.load(show_id="YOUR_SHOW_ID")
    uploader = SpotifyCreatorsUploader(config)
    episode = await uploader.publish(
        EpisodeRequest(
            video_path=Path("episode.mp4"),
            title="Paper title",
            description="Episode description",
            thumbnail_path=Path("thumbnail.png"),  # optional, 16:9 PNG or JPEG
        ),
        on_progress=lambda fraction, detail: print(f"{fraction:.0%} {detail}"),
    )
    print(episode.status.value, episode.episode_id, episode.url)


asyncio.run(main())
```

`UploaderConfig(home=...)` points the library at its own state directory, so an application can keep
its session apart from the CLI's. `interactive_login`, `load_storage_state`/`summarize` and
`probe_media`/`spec_issues`/`prepare_for_spotify` are exported for the same reason.

`UploaderConfig.load()` also reads `$SPOTIFY_SHOW_ID` and the stored show id. `publish()` accepts
`dry_run`, `force`, `resume_episode_id` and `on_progress`, and returns a `PublishedEpisode`. The
package ships type information (`py.typed`); the public names are listed in
`spotify_uploader.__all__`.

## Servers and CI

Sign in once on a machine with a screen, then move the session to the server:

- preferred: `spotify-uploader import-session cookies.json` on the server; or
- `scp` `~/.spotify-uploader/storage_state.json` and `chmod 600` it.

`SPOTIFY_UPLOADER_HOME` overrides the state directory, `SPOTIFY_SHOW_ID` supplies a default show id,
and `SPOTIFY_UPLOADER_CHROME` points at a Chrome executable. `status` is strictly offline; only
`check`, `doctor`, `shows` and `upload` use the network.

## Exit codes and errors

| Code | Meaning |
| ---- | ------- |
| 0 | success |
| 2 | `ConfigurationError`, `ValidationError`, `ShowSetupError` or an argparse error — fix flags/files/setup; also a failed `doctor` |
| 3 | `AuthExpiredError` — run `spotify-uploader login` again |
| 4 | `UploadError` or any other `UploaderError` — the upload or a command failed |

Every handled error prints exactly one JSON line to stderr:

```json
{"error": "UploadError", "message": "...", "step": "publish", "artifacts": null}
```

`step` and `artifacts` are set only for a wizard failure, and `ShowSetupError` adds
`draft_episode_id`. Success payloads are JSON on stdout, so cron/systemd can branch on the exit code
and parse stdout safely.

## State layout and security

```text
~/.spotify-uploader/          0700
  storage_state.json          0600 Playwright session (sp_dc cookie; full account access)
  config.json                 0600 stored show id, optional channel
  ledger.json                 0600 SHA-256 -> PublishedEpisode idempotency ledger
  chrome-profile/             separate Chrome profile used only for interactive login
```

**SECURITY:** the `sp_dc` session is a **full-account credential** — treat it like a password. It
is stored mode 0600 in a 0700 directory, is gitignored, and must never be committed, pasted into a
ticket or copied into an image. Revoke it by signing out (or re-running `login`), not by editing.

## What was verified live (2026-11-06 and 2026-11-07)

Against a real personal account, headless, with a saved session: `check` passes, `upload --dry-run`
completes in about 15 seconds, and a **real publish works end to end** (a 5 min, 128 MB video:
upload, Spotify processing, publish; the episode page then read "Published" and the episodes list
showed it with status Published). Re-running the same file is skipped from the local ledger without
opening a browser.

Verified again on 2026-11-07 (same account, two shows): a **custom thumbnail** set during a dry run
(`--thumbnail`, a 1280x720 PNG) is saved when the wizard's Next button is clicked and survives a fresh
page load, where Spotify serves it from its own CDN at 1280 px; `shows` and `doctor` ran against the
live account (the show's name and setup state were read correctly). A real publish *with* a thumbnail
was not run end to end; the thumbnail and publish steps were each verified separately.

Things the live site taught us (all encoded in `selectors.py` and tested):

- Titles are limited to **200** characters.
- The details form appears while the video is still uploading; only "Preview ready!" means Spotify
  finished. That text exists twice in the DOM and the first copy is invisible, so visibility checks
  must filter to visible elements.
- The episode id is in the wizard URL as soon as the file is chosen, so an id in the URL does
  **not** prove a publish happened; success means the page left the wizard.
- A cookie overlay (`#onetrust-consent-sdk`) intercepts clicks and is removed first.
- Explicit content is a switch (`input[name=podcastEpisodeIsExplicit]`).
- **Every upload creates a draft** on the Spotify side, including `--dry-run` and any run that fails
  after the upload. Delete leftover drafts in the Episodes list.
- The wizard form (title, description, thumbnail) is only saved when **Next** is clicked, so the
  thumbnail is chosen before Next and checked afterwards. The thumbnail input shares the id
  `uploadAreaInput` with the video input; it is selected as `#thumbnail-list input[type=file]`.
- Spotify's dashboard sidebar sometimes fails to start (the page logs `reading 'TenantFeatures'`),
  so the show name is also read from the page's first large title, and other shows cannot always be
  discovered. `/home/show` with no id opens the last show you used.

## Limitations

- The selectors are tied to Spotify for Creators' current **English** UI. A redesign will break
  them; run `spotify-uploader doctor` first when something stops working.
- Only **macOS** was exercised live, with a personal account. Other platforms and account types are
  untested.
- Spotify offers no all-shows page, so `shows` cannot promise to find every show; pass `--show-id`.
- Not yet on PyPI under any name; the install commands above apply once it is released.
- Spotify can change or restrict automated access at any time; this project cannot promise that an
  upload will keep working.

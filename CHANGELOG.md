# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-07

Initial public release.

### Added

- The library also exports `interactive_login`, session status helpers
  (`load_storage_state`, `summarize`) and the video helpers (`probe_media`,
  `spec_issues`, `prepare_for_spotify`), so a host application can run the whole
  flow without shelling out to the CLI.
- `spotify-uploader` command-line tool and importable `spotify_uploader`
  library for publishing video podcast episodes to Spotify for Creators
  through a saved headless Playwright session.
- One-time interactive login that saves a reusable session, plus
  `import-session` for a Playwright `storage_state.json` or a browser-exported
  cookie list.
- `status` for offline session/show/ledger state and `check` for a headless,
  online session check.
- `shows` to list the account's shows, select a default show, and guard
  publishes against a show whose setup is unfinished; `--show-id` overrides
  per command and `--home` keeps a second Spotify account separate.
- `prepare` to re-encode a video to Spotify's video specs, with a
  report-only `--check` mode.
- `upload` with title/description, explicit-content flag, custom 16:9
  thumbnail, `--dry-run`, `--force`, `--resume-draft`, `--debug-dir` failure
  artifacts, progress on stderr, and a JSON result on stdout.
- `doctor` to diagnose the install, saved session, show, and (with
  `--rehearse`) the whole upload wizard without publishing.
- `install-browser` to install the Chromium build Playwright drives.
- SHA-256 idempotency ledger that skips a video already published from this
  machine.
- Stable exit codes and one-line JSON error output for scripting and cron.
- Typed public Python API: `SpotifyCreatorsUploader`, `UploaderConfig`,
  `EpisodeRequest`, `PublishedEpisode`, `PublishStatus`, `ShowInfo`, and the
  package error types, shipped with `py.typed`.

### Notes

- Unofficial project; it automates Spotify for Creators' web UI because no
  public upload API exists. It can break when Spotify changes the site and may
  conflict with Spotify's terms or guidelines on automated access.
- Verified live only on macOS against a personal account.

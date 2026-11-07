# Releasing

Releases go to PyPI through GitHub Actions with
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/): there is no API token to
store, rotate or leak. A published version can never be uploaded again under the same number,
so check before you tag.

## One-time setup

1. On pypi.org open **Your account → Publishing → Add a new pending publisher** and enter:

   | Field | Value |
   | ----- | ----- |
   | PyPI project name | `spotify-creators-uploader` |
   | Owner | `Jayashakthi28` |
   | Repository name | `spotify-creators-uploader` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

2. On GitHub open **Settings → Environments → New environment**, name it `pypi`, and (optional
   but recommended) add yourself as a required reviewer so every release needs a click.

## Each release

1. Bump `version` in `pyproject.toml` and add a `CHANGELOG.md` entry; commit to `main`.
2. Wait for CI to pass on that commit.
3. Tag and push: `git tag v0.1.0 && git push origin v0.1.0`. The tag must equal `v` plus the
   `pyproject.toml` version, or the workflow stops before building.
4. Approve the `pypi` environment if you required a reviewer. The workflow builds the sdist and
   wheel, runs `twine check --strict`, and publishes.
5. Check <https://pypi.org/project/spotify-creators-uploader/> and run
   `pip install spotify-creators-uploader` in a fresh environment.

## Local dry run

```bash
make build                      # uv build
uvx twine check --strict dist/*
```

# Compatibility extensions for bili-agent 0.7.0

This branch preserves the original MIT-licensed kukemc/bili-cli source and history, based on upstream tag v0.7.0 (cc6dfeea60edc3b853e6cfa51cdb9e58e83f3283). The upstream LICENSE remains unchanged.

Additional top-level modules provide guarded compatibility fixes without modifying installed dependency files:

- coin_form_patch.py sends the documented select_like form parameter for coin requests, preserving the separate like endpoint.
- response_diagnostics_patch.py and bili_safe_diagnostics.py minimize error output, redact unknown fields and credential-like data, and do not add retries or alter identity.
- launch.py applies these fixes before starting the standard CLI with HTTPX transport.

Install this checkout in a virtual environment with `python -m pip install -e .` and run `python launch.py --help`. The compatibility patches intentionally fail closed unless bili-agent is exactly 0.7.0 and bilibili-api-python is exactly 17.4.2 with the expected method source hashes. Review and update the guards before using another version.

Run the extension tests separately in fresh processes because the patches replace process-global methods:

    python -m unittest discover -s tests -p test_coin_form_patch.py
    python -m unittest discover -s tests -p test_response_diagnostics_patch.py

These tests use mock HTTP responses and dummy credentials. No credentials, runtime state, private messages, logs, model weights, recordings, transcripts, or signed media URLs are included.

## Live and optional experimental source

See [LIVE.md](LIVE.md) and [LIVE_DANMAKU.md](LIVE_DANMAKU.md) for bounded live transcription, danmaku reception, and explicitly invoked one-shot sending. The launcher registers these commands. Use `python launch.py` wherever examples use `bili`. Live tests are offline/synthetic; no real-room or production streaming validation is claimed. [LIVE_VERSIONS.json](LIVE_VERSIONS.json) records the reviewed dependency versions.

The disabled speaker-diarization prototype and synthetic tests are preserved under [optional/experimental](optional/experimental/README.md), outside the default launcher.

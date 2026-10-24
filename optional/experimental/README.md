# Experimental speaker diarization (disabled)

This directory preserves an optional CPU speaker-clustering integration. It is not installed as a package, imported, or registered by the default launcher. The standard Qwen/VAD transcription path remains unchanged.

The source targets bili-agent 0.7.0, sherpa-onnx 2.0.0, NumPy, ffmpeg, CPU Qwen3-ASR, and separately acquired segmentation/embedding models. No model weights or recordings are distributed here. Model paths are resolved relative to this module. The resource module makes this prototype Unix-specific.

Speaker labels are file-local model estimates, not real identities. Overlap attribution and segmentation are imperfect. This prototype has offline mocked regression coverage, not a guarantee of transcription or speaker accuracy.

Run its synthetic offline tests with:

    python optional/experimental/tests/test_speaker_diarization.py

Do not register this module by default. Review its implementation, dependencies, model licenses, and input/output handling before explicit experimental use.

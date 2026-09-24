# Optional ad ending for before/after comparisons

CutKit 3.4.0 adds **添加广告投放结尾** in the comparison parameters. It is off
by default; its state is remembered and included in comparison presets.

When enabled, the complete comparison is rendered first. Its final decoded frame
is frozen for the ending duration and the ending is alpha-composited over it.
The body audio ends at the cut, followed by the ending's own audio. Missing audio
is padded with silence. At 30 fps, the supplied ending adds 86 frames (2.867 s).

`assets/ad-ending.webm` is a compact VP9-alpha/Opus derivative of the user's
`压缩-结尾.mov` (1080×1920, 60 fps, QTRLE/ARGB with AAC audio). The original is
preserved. The derivative ships in the app; no external-volume path is required.
Use the libvpx-vp9 decoder to retain alpha. The compositor normalizes the body
frame rate before tpad so the frozen tail contains actual video frames.

The final output is written separately from the temporary comparison. Cancellation
terminates tracked encoders and removes incomplete final output. Existing output
names receive numeric suffixes through the comparison's existing output flow.

Validation: `python3 tests/test_ad_ending.py` checks full video frame count,
last-frame freezing, transparent regions, ending audio timing, short BGM handling,
and HTTP routing with the option enabled/disabled. It also checks that running a
comparison preserves settings owned by other modes.

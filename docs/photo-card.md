# Photo card + sticker

In the existing 圆环箭头 tab, 角标样式 selects the original ring/arrow or 照片卡片＋.
The new design follows the supplied reference: a 3:4 portrait card with rounded
corners, a white rim, and an overlapping white circle with a dark plus at bottom
right. The default width is 24.5% of the canvas, centred at (16.6%, 74.5%).
The surrounding canvas remains transparent. No caption is baked into the sticker.

Crop focus, card width, placement, canvas dimensions, duration and optional pop-in
are adjustable. Preview and export share the same build_badge drawing function.
Outputs are ProRes 4444 MOV plus a transparent PNG, with unique filenames and a
separate photo-card history label. The pop-in anchor follows the selected position.

Validation: tests/test_photo_card.py covers transparency, photo/rim/plus pixels,
real MOV decoding and duration, HTTP style routing, zero crop positions, invalid
parameters, and output naming. Existing ring rendering remains the default.

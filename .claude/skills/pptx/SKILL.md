---
name: pptx
description: PowerPoint presentation creation, editing, and analysis — PptxGenJS for new decks, unpack/edit for existing, markitdown for reading
user-invocable: true
---

# PPTX Skill

## Quick Reference

| Task | Guide |
|------|-------|
| Read/analyze content | `python -m markitdown presentation.pptx` |
| Edit or create from template | Unpack -> manipulate slides -> edit content -> pack |
| Create from scratch | Use PptxGenJS |

---

## Reading Content

```bash
python -m markitdown presentation.pptx

# Visual overview
python scripts/thumbnail.py presentation.pptx

# Raw XML
python scripts/office/unpack.py presentation.pptx unpacked/
```

---

## Creating from Scratch

Use PptxGenJS (`npm install -g pptxgenjs`) when no template is available.

---

## Design Ideas

**Don't create boring slides.** Plain bullets on a white background won't impress anyone.

### Before Starting

- **Pick a bold, content-informed color palette**
- **Dominance over equality**: One color should dominate (60-70%)
- **Dark/light contrast**: Dark backgrounds for title + conclusion, light for content
- **Commit to a visual motif**: ONE distinctive element repeated across slides

### Color Palettes

| Theme | Primary | Secondary | Accent |
|-------|---------|-----------|--------|
| **Midnight Executive** | `1E2761` | `CADCFC` | `FFFFFF` |
| **Forest & Moss** | `2C5F2D` | `97BC62` | `F5F5F5` |
| **Coral Energy** | `F96167` | `F9E795` | `2F3C7E` |
| **Warm Terracotta** | `B85042` | `E7E8D1` | `A7BEAE` |
| **Ocean Gradient** | `065A82` | `1C7293` | `21295C` |
| **Charcoal Minimal** | `36454F` | `F2F2F2` | `212121` |

### Typography

| Element | Size |
|---------|------|
| Slide title | 36-44pt bold |
| Section header | 20-24pt bold |
| Body text | 14-16pt |
| Captions | 10-12pt muted |

### Avoid

- Don't repeat the same layout across slides
- Don't center body text - left-align paragraphs and lists
- Don't default to blue - pick colors that reflect the topic
- Don't create text-only slides - add images, icons, charts
- NEVER use accent lines under titles - hallmark of AI-generated slides

---

## QA (Required)

**Assume there are problems. Your job is to find them.**

### Content QA

```bash
python -m markitdown output.pptx
```

### Visual QA

Convert to images and inspect:

```bash
python scripts/office/soffice.py --headless --convert-to pdf output.pptx
pdftoppm -jpeg -r 150 output.pdf slide
```

Look for: overlapping elements, text overflow, low contrast, uneven gaps, insufficient margins.

### Verification Loop

1. Generate slides -> Convert to images -> Inspect
2. List issues found
3. Fix issues
4. Re-verify affected slides
5. Repeat until clean

---

## Converting to Images

```bash
python scripts/office/soffice.py --headless --convert-to pdf output.pptx
pdftoppm -jpeg -r 150 output.pdf slide
```

---

## Dependencies

- `pip install "markitdown[pptx]"` - text extraction
- `pip install Pillow` - thumbnail grids
- `npm install -g pptxgenjs` - creating from scratch
- LibreOffice (`soffice`) - PDF conversion
- Poppler (`pdftoppm`) - PDF to images

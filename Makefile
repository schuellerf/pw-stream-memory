# Rasterize the Inkscape SVG. Other SVG engines (rsvg, ql, browsers) do not
# match Inkscape on filters, stroke alignment, and Inkscape-only attributes.
# CI does not re-export (Inkscape versions differ); it checks svg.sha256.

INKSCAPE ?= inkscape
SVG := src/pw_stream_memory/data/pw-stream-memory.svg
SVG_STAMP := assets/svg.sha256
LOGO_PNG := assets/logo.png
ICON_PNG := src/pw_stream_memory/data/pw-stream-memory.png

.PHONY: generate check-pngs
generate: $(LOGO_PNG) $(ICON_PNG)
	sha256sum $(SVG) > $(SVG_STAMP)

# Fail if the SVG changed since the last 'make generate' (no Inkscape needed).
check-pngs:
	@test -f $(LOGO_PNG) && test -f $(ICON_PNG) || { \
		echo >&2 "PNG files are missing; run 'make generate'."; \
		exit 1; \
	}
	@sha256sum --strict -c $(SVG_STAMP) || { \
		echo >&2 "SVG changed; run 'make generate' and commit the PNGs and $(SVG_STAMP)."; \
		exit 1; \
	}

$(LOGO_PNG): $(SVG) | assets
	$(INKSCAPE) --export-area-page --export-type=png \
		--export-width=512 --export-height=512 \
		--export-filename=$@ $<

$(ICON_PNG): $(SVG)
	$(INKSCAPE) --export-area-page --export-type=png \
		--export-width=256 --export-height=256 \
		--export-filename=$@ $<

assets:
	mkdir -p $@

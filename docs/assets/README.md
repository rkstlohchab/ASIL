# docs/assets

Binary artifacts the main README references. Committed to the repo so
GitHub renders them inline; replaced with each new release.

## `asil-demo.gif`

The 90-second tour rendered at the top of the project README.
Regenerate any time the CLI surface changes:

```bash
asciinema rec docs/assets/asil-demo.cast
make demo-auto
# Ctrl+D after "Tour complete"
agg --speed 1.5 docs/assets/asil-demo.cast docs/assets/asil-demo.gif
```

Target size: 2–4 MB so GitHub + Medium render it without complaint.

## `asil-demo.cast`

The asciinema source for `asil-demo.gif`. Keep it in version control —
swapping `--speed`, `--theme`, or `--font-size` later only re-runs
`agg`, not the whole recording.

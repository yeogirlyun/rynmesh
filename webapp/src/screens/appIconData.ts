// One drawn icon per app. Each icon is up to five layers on a 48x48 grid:
//   back  - a lighter sheet or shape behind the subject
//   main  - the white subject
//   mark  - solid detail in the app's colour
//   line  - fine detail strokes in the app's colour
//   glyph - white stroked detail (handles, stands, waveform)
// The tile gradient and detail colour come from the app's tone (see craft-apps-refresh.css).
// Shared by the Apps page and scripts/generate-app-icons.mjs, which draws the
// Start menu, Launchpad and window icons from the same layers.
export type Layers = { back?: string; main?: string; mark?: string; line?: string; glyph?: string; label?: string; size?: number; y?: number };

export const PAGE = "M14 7h13l9 9v23a2 2 0 0 1-2 2H14a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2z";
export const PAGE_BACK = "M19 4h12a2 2 0 0 1 1.4.6L41 13a2 2 0 0 1 .6 1.4V36a2 2 0 0 1-2 2H19a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z";

export const layers: Record<string, Layers> = {
  pdfcraft: { back: PAGE_BACK, main: PAGE, line: "M27 7v7a2 2 0 0 0 2 2h7M17 21h14M17 25h9", label: "PDF", size: 9.5, y: 35 },
  wordcraft: { back: PAGE_BACK, main: PAGE, line: "M27 7v7a2 2 0 0 0 2 2h7M17 20h9", label: "W", size: 18, y: 36 },
  gridcraft: {
    back: "M16 6h24a2 2 0 0 1 2 2v24a2 2 0 0 1-2 2H16a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2z",
    main: "M12 10h24a2 2 0 0 1 2 2v26a2 2 0 0 1-2 2H12a2 2 0 0 1-2-2V12a2 2 0 0 1 2-2z",
    mark: "M20.5 19h9v8h-9z",
    line: "M10 19h28M10 27h28M10 35h28M20.5 10v30M29.5 10v30",
  },
  deckcraft: {
    back: "M12 7h32a2 2 0 0 1 2 2v20a2 2 0 0 1-2 2H12a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2z",
    main: "M8 11h32a2 2 0 0 1 2 2v20a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V13a2 2 0 0 1 2-2z",
    mark: "M30 17.5a6.5 6.5 0 1 0 6.5 6.5H30zM32 15.5a6.5 6.5 0 0 1 6.5 6.5H32z",
    line: "M11 18h10M11 23h10M11 28h6",
    glyph: "M24 37v4M17 42h14",
  },
  cadcraft: {
    back: "M40 14a8 8 0 1 1-16 0 8 8 0 0 1 16 0z",
    main: "M11 37V13a1 1 0 0 1 1.7-.7l24.2 24.2a1 1 0 0 1-.7 1.7H12a1 1 0 0 1-1-1z",
    mark: "M16 33V22.5L26.5 33z",
    line: "M11 19h4M11 25h3M11 31h4",
  },
  designcraft: {
    back: "M17 4h22a2 2 0 0 1 2 2v30a2 2 0 0 1-2 2H17a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z",
    main: "M12 7h22a2 2 0 0 1 2 2v30a2 2 0 0 1-2 2H12a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2z",
    mark: "M15 11h16a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H15a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2z",
    line: "M13 28h9M13 32h9M13 36h6M26 28h7M26 32h7M26 36h5",
  },
  figcraft: {
    back: "M12 7h32a2 2 0 0 1 2 2v22a2 2 0 0 1-2 2H12a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2z",
    main: "M8 11h32a2 2 0 0 1 2 2v22a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V13a2 2 0 0 1 2-2z",
    mark: "M10.5 21.5h11v9h-11zM28 24l10 4.4-4.2 1.6-1.6 4.2z",
    line: "M6 18h36M25 22h11",
  },
  // Photo editing
  lightcraft: {
    back: "M12 7h30a2 2 0 0 1 2 2v24a2 2 0 0 1-2 2H12a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2z",
    main: "M8 11h30a2 2 0 0 1 2 2v24a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V13a2 2 0 0 1 2-2z",
    mark: "M6 35l10-12 8 9 5-6 11 9v2a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2zM33 19a3.5 3.5 0 1 1-7 0 3.5 3.5 0 0 1 7 0z",
  },
  // Image editing
  photocraft: {
    back: "M24 25l16 8-16 8-16-8zM24 16l16 8-16 8-16-8z",
    main: "M24 7l16 8-16 8-16-8z",
    mark: "M24 11l8 4-8 4-8-4z",
  },
  vectorcraft: {
    main: "M9 31c0-9 6-17 16-17 8 0 13 5 13 12 0 8-6 13-14 13-9 0-15-3-15-8z",
    mark: "M7 29h4v4H7zM13 17h4v4h-4zM37 15h4v4h-4zM36.5 24h4v4h-4z",
    line: "M9 31L15 19M38 26l3-9",
  },
  soundcraft: {
    back: "M7 12h34a3 3 0 0 1 3 3v18a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V15a3 3 0 0 1 3-3z",
    glyph: "M11 20v8M17.5 15v18M24 10v28M30.5 16v16M37 21v6",
  },
  effectcraft: {
    back: "M14 9l1.5 3.8 3.8 1.5-3.8 1.5L14 19.6l-1.5-3.8-3.8-1.5 3.8-1.5z",
    main: "M32 5l2.6 6.4L41 14l-6.4 2.6L32 23l-2.6-6.4L23 14l6.4-2.6z",
    glyph: "M8 40L25 23M37 29v7M33.5 32.5h7",
  },
  filmcraft: {
    back: "M8 16L6.6 8.4l30.8-4.8L39 11.2z",
    main: "M8 19h32v19a2 2 0 0 1-2 2H10a2 2 0 0 1-2-2z",
    mark: "M20 24.5l10 5.5-10 5.5z",
    line: "M14.5 14.5l-1.2-6.2M23 13l-1.2-6.2M31.5 11.6l-1.2-6.2",
  },
  resume: {
    back: PAGE_BACK,
    main: PAGE,
    mark: "M26 19a4 4 0 1 1-8 0 4 4 0 0 1 8 0zM15 32a7 7 0 0 1 14 0z",
    line: "M27 7v7a2 2 0 0 0 2 2h7M16 36h14",
  },
  recruit: {
    back: "M42 12a5 5 0 1 1-10 0 5 5 0 0 1 10 0z",
    main: "M10 19h28a2 2 0 0 1 2 2v15a2 2 0 0 1-2 2H10a2 2 0 0 1-2-2V21a2 2 0 0 1 2-2z",
    mark: "M21.5 26.5h5v5h-5z",
    line: "M8 27h32",
    glyph: "M18 19v-3a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v3",
  },
  desk: {
    main: "M8 27h5a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-1a4 4 0 0 1-4-4zM40 27h-5a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h1a4 4 0 0 0 4-4z",
    glyph: "M11 28v-4a13 13 0 0 1 26 0v4M38 38c0 4-4 5-10 5",
  },
  nas: {
    main: "M9 10h30a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2zM9 26h30a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2z",
    mark: "M16 16a2 2 0 1 1-4 0 2 2 0 0 1 4 0zM16 32a2 2 0 1 1-4 0 2 2 0 0 1 4 0z",
    line: "M22 16h13M22 32h13",
  },
};

export const tones: Record<string, string> = {
  pdfcraft: "red", wordcraft: "blue", gridcraft: "green", deckcraft: "orange",
  cadcraft: "slate", designcraft: "rose", figcraft: "violet", lightcraft: "amber",
  photocraft: "fuchsia", vectorcraft: "cyan", soundcraft: "teal", effectcraft: "indigo",
  filmcraft: "slate", resume: "cyan", recruit: "violet", desk: "green", nas: "slate",
};

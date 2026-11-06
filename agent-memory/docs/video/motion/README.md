The 60-second Agent memory explainer uses native 3840 × 2160 product illustrations, actual Framer Motion components and easing, and Remotion's deterministic video renderer. Prompts type on screen, cursors click, nodes emerge, edge packets flow, panels enter, and forgotten preferences disappear. The camera makes restrained pushes and pulls; captions and branding stay steady. It explains one growing user-memory graph across chats and tools, with the existing code graph consulted separately after memory retrieval. It is an illustrative explanation rather than live agent footage. Narration is an AI-generated British female voice.

The composition is 1440 frames at 24 fps. Every transform is calculated from `useCurrentFrame()`, using Framer Motion's `cubicBezier()` easing and `motion.div` transform styles, so export does not depend on wall-clock animation timing.

Each scene has a distinct layout and motion: an orbital intro, CLI typing with a horizontal capture handoff, radial memory assembly, a targeted correction and graph recentering, sequential retrieval with separate memory/code/work visuals, a wide dashboard pan with inspector reveal, and a closing node convergence into the brand symbol. Branding and captions remain steady throughout.

To rebuild on this Mac:

```sh
cd docs/video/motion
npm install
npm run samples
npm run render
```

Inputs are `../voiceover.wav` and `../voiceover.srt`. The renderer stages the WAV under the operating system's temporary directory. It loads the installed DM Sans fonts from `/Users/tirth/Library/Fonts`. Change that font directory in `render.mjs` on another machine, or provide equivalent local fonts. Set `VIDEO_CHROME_PATH` for a different installed Chrome executable. `VIDEO_VOICEOVER_PATH` optionally sets the final audio source, read after visual rendering completes.

Output: `../code-review-graph-agent-memory-4k.mp4`. The final local ffmpeg mux fixes AAC packet padding so both the video and container are exactly 60 seconds. It copies the rendered video without another video encode. No API key is required. The renderer only consumes local authored media.

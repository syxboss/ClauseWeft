
function drawSignalField() {
  const canvas = document.querySelector(".signal-field");
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const w = canvas.width, h = canvas.height;
  const palette = getComputedStyle(canvas);
  const back = palette.getPropertyValue("--signal-back").trim();
  const middle = palette.getPropertyValue("--signal-middle").trim();
  const front = palette.getPropertyValue("--signal-front").trim();
  ctx.clearRect(0, 0, w, h);
  // Project a folded sheet of parallel lines. Colour varies along each line,
  // leaving the darker return fold visible through the brighter front edge.
  for (let lane = 0; lane < 112; lane++) {
    const v = lane / 111, depth = Math.sin(Math.PI * v);
    const shade = ctx.createLinearGradient(0, 0, w, 0);
    shade.addColorStop(0, `rgba(${back},.03)`);
    shade.addColorStop(.24, `rgba(${back},${.18 + .35 * depth})`);
    shade.addColorStop(.55, `rgba(${middle},${.35 + .42 * depth})`);
    shade.addColorStop(.83, `rgba(${front},${.50 + .36 * depth})`);
    shade.addColorStop(1, `rgba(${front},.15)`);
    ctx.strokeStyle = shade;
    ctx.lineWidth = lane % 8 === 0 ? 1.65 : .85;
    ctx.beginPath();
    for (let step = 0; step <= 250; step++) {
      const t = step / 250;
      const crest = Math.exp(-(((t - .68 - .14 * v) / .235) ** 2));
      const fold = Math.sin(t * 8.0 + v * 4.5 + .4);
      const x = w * (-.04 + 1.08*t + .05 * Math.sin(v * Math.PI * 2 + t * 5) * crest);
      const height = .56 + (v-.5)*.39 + .14*Math.sin(t*6 + v*1.7)
                     - crest*(.27 + .25*fold) + .07*Math.cos(v*6 + t*4);
      const y = h * (.19 + .75*height);
      if (!step) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    }
    ctx.stroke();
  }
}
drawSignalField();
window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", drawSignalField);

import React, {useEffect, useState} from 'react';
import {motion, cubicBezier} from 'framer-motion';
import {AbsoluteFill, Audio, continueRender, delayRender, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';

const INK = '#202522';
const BG = '#f7f7f3';
const TEAL = '#087f71';
const TIMES = [0, 7, 17, 26, 37, 48, 56, 60];
const smoothCamera = cubicBezier(0.42, 0, 0.58, 1);
const softEntrance = cubicBezier(0.22, 0.61, 0.36, 1);
const clamp = (n) => Math.max(0, Math.min(1, n));

// Continuous camera keyframes preserve the illustrated scene crossfades.
// Small pushes reveal the graph; gentle pulls restore the wider context.
const CAMERA = [
  {t: 0, scale: 1.015, x: 0, y: 0, ox: 0.5, oy: 0.44},
  {t: 6.65, scale: 1.070, x: 6, y: -10, ox: 0.5, oy: 0.44},
  {t: 7.55, scale: 1.035, x: 4, y: 0, ox: 0.51, oy: 0.44},
  {t: 16.65, scale: 1.085, x: 8, y: -12, ox: 0.51, oy: 0.44},
  {t: 17.55, scale: 1.045, x: 0, y: 0, ox: 0.5, oy: 0.46},
  {t: 25.65, scale: 1.110, x: 0, y: -8, ox: 0.5, oy: 0.46},
  {t: 26.55, scale: 1.100, x: 0, y: -8, ox: 0.5, oy: 0.46},
  {t: 36.65, scale: 1.035, x: 0, y: 0, ox: 0.5, oy: 0.46},
  {t: 37.55, scale: 1.035, x: 0, y: 0, ox: 0.5, oy: 0.44},
  {t: 47.65, scale: 1.088, x: 0, y: -10, ox: 0.5, oy: 0.44},
  {t: 48.55, scale: 1.080, x: 8, y: -6, ox: 0.505, oy: 0.46},
  {t: 55.65, scale: 1.035, x: 0, y: 0, ox: 0.5, oy: 0.46},
  {t: 56.55, scale: 1.075, x: 0, y: -8, ox: 0.5, oy: 0.46},
  {t: 60, scale: 1.020, x: 0, y: 0, ox: 0.5, oy: 0.46},
];

export function cameraAt(t) {
  const last = CAMERA[CAMERA.length - 1];
  if (t >= last.t) return last;
  const index = CAMERA.findIndex((point, i) => i < CAMERA.length - 1 && t >= point.t && t < CAMERA[i + 1].t);
  const a = CAMERA[Math.max(0, index)];
  const b = CAMERA[Math.max(0, index) + 1];
  const p = smoothCamera(clamp((t - a.t) / (b.t - a.t)));
  return Object.fromEntries(['scale', 'x', 'y', 'ox', 'oy'].map((key) => [key, a[key] + (b[key] - a[key]) * p]));
}

function Caption({cue, time}) {
  if (!cue) return null;
  const enter = softEntrance(clamp((time - cue.start) / 0.12));
  const exit = softEntrance(clamp((cue.end - time) / 0.09));
  return <motion.div initial={false} style={{
    position: 'absolute', bottom: 94, left: 230, right: 230,
    display: 'flex', justifyContent: 'center', pointerEvents: 'none',
    opacity: Math.min(enter, exit), y: 5 * (1 - enter),
  }}>
    <div style={{
      backgroundColor: 'rgba(236, 239, 232, 0.98)',
      border: '2px solid rgba(220, 226, 220, 0.8)', borderRadius: 36,
      padding: '22px 76px 25px', maxWidth: 3220, textAlign: 'center',
      color: INK, fontSize: 64, lineHeight: 1.23, fontWeight: 400,
      whiteSpace: 'pre-line', boxShadow: '0 6px 22px rgba(32, 37, 34, 0.035)',
    }}>{cue.text}</div>
  </motion.div>;
}

const panel = {background: '#fff', border: '3px solid #dce2dc', borderRadius: 34, boxShadow: '0 18px 55px #2025220a'};
const easeIn = (time, at, duration = 0.6) => softEntrance(clamp((time - at) / duration));
const typed = (text, time, at, duration) => text.slice(0, Math.floor(text.length * clamp((time - at) / duration)));
const mix = (a, b, p) => a + (b - a) * p;
const BLUE = '#5c6f99';

function Cursor({x, y, time, clickAt, opacity = 1}) {
  const click = Math.max(0, 1 - Math.abs(time - clickAt) / 0.45);
  return <div style={{position: 'absolute', left: x, top: y, zIndex: 8, opacity, transform: `scale(${1 - click * 0.16})`}}>
    {click > 0 && <div style={{position: 'absolute', left: -38, top: -38, width: 88 + click * 55, height: 88 + click * 55, border: '5px solid #087f7180', borderRadius: '50%', opacity: click}} />}
    <svg width="80" height="98" viewBox="0 0 44 54"><path d="M4 3L38 30L22 33L17 49L4 3Z" fill={INK} stroke="white" strokeWidth="3"/></svg>
  </div>;
}

function Heading({title, subtitle, time, at, center = false}) {
  const p = easeIn(time, at, 0.65);
  return <motion.div initial={false} style={{position: 'absolute', left: 280, top: 340, width: 3280, opacity: p, x: center ? 0 : -35 * (1 - p), y: center ? -25 * (1 - p) : 0, textAlign: center ? 'center' : 'left'}}>
    <div style={{fontSize: 83, fontWeight: 700, letterSpacing: -2.2, lineHeight: 1.16}}>{title}</div>
    <div style={{fontSize: 36, color: '#707872', marginTop: 35}}>{subtitle}</div>
  </motion.div>;
}

function GraphNode({x, y, label, detail = 'From your prompt', width = 620, height = 205, opacity = 1, scale = 1, selected = false, color = TEAL}) {
  return <g opacity={opacity} transform={`translate(${x} ${y}) scale(${scale})`}>
    <rect x={-width / 2} y={-height / 2} width={width} height={height} rx="30" fill={selected ? '#e7f5ef' : '#fff'} stroke={selected ? color : '#b5c4bb'} strokeWidth={selected ? 5 : 3}/>
    <circle cx={-width / 2 + 43} cy={-height / 2 + 42} r="9" fill={color}/>
    <text x={-width / 2 + 69} y={-height / 2 + 52} fontSize="28" fill="#707872">Preference</text>
    <text x={-width / 2 + 34} y="17" fontSize="43" fontWeight="700" fill={INK}>{label}</text>
    <text x={-width / 2 + 34} y={height / 2 - 28} fontSize="27" fill="#707872">{detail}</text>
  </g>;
}

function MemoryCore({x, y, radius = 170, time, label = 'Your memory', sub = 'Across chats and tools'}) {
  return <g><circle cx={x} cy={y} r={radius + 22 + Math.sin(time * 1.7) * 5} fill="none" stroke="#087f7112" strokeWidth="28"/>
    <circle cx={x} cy={y} r={radius} fill={TEAL}/>
    <text x={x} y={y - 17} textAnchor="middle" fill="white" fontSize="44" fontWeight="700">{label}</text>
    <text x={x} y={y + 38} textAnchor="middle" fill="#d7eee6" fontSize="27">{sub}</text>
  </g>;
}

function Packet({a, b, time, phase = 0, color = TEAL, opacity = 1}) {
  const p = (time * 0.27 + phase) % 1;
  return <circle cx={mix(a[0], b[0], p)} cy={mix(a[1], b[1], p)} r="12" fill={color} opacity={opacity}/>;
}

// Scene 1: an orbital reveal, with independent tool chips gathering around one memory.
function SceneIntro({time}) {
  const gather = easeIn(time, 0.45, 2.1);
  const tools = ['CLI', 'Coding agent', 'Editor'];
  return <>
    <motion.div initial={false} style={{position: 'absolute', left: 280, top: 565, width: 1370, opacity: easeIn(time, 0.15, 0.9), y: 40 * (1 - easeIn(time, 0.15, 0.9))}}>
      <div style={{fontSize: 118, lineHeight: 1.05, letterSpacing: -3.8, fontWeight: 700}}>See what your<br/>agent remembers.</div>
      <div style={{fontSize: 46, color: '#707872', lineHeight: 1.35, marginTop: 76, maxWidth: 1150}}>One shared memory that follows you across your chats and tools.</div>
      <div style={{marginTop: 105, fontSize: 42, color: TEAL, opacity: easeIn(time, 3.8, 0.7)}}>Memory you can actually control.</div>
    </motion.div>
    <svg width="3840" height="2160" viewBox="0 0 3840 2160" style={{position: 'absolute', inset: 0}}>
      <g opacity={gather}>
        <ellipse cx="2680" cy="1100" rx="660" ry="485" fill="none" stroke="#dce6dc" strokeWidth="3"/>
        <ellipse cx="2680" cy="1100" rx="445" ry="340" fill="none" stroke="#dce6dc" strokeWidth="3" strokeDasharray="8 17"/>
        {Array.from({length: 12}, (_, i) => {
          const angle = i * Math.PI / 6 + time * 0.12;
          const r = mix(790, 355, gather);
          return <circle key={i} cx={2680 + Math.cos(angle) * r} cy={1100 + Math.sin(angle) * r * 0.75} r={10 + i % 3 * 3} fill={TEAL} opacity={0.35 + i % 3 * 0.2}/>;
        })}
        {tools.map((tool, i) => {
          const angle = time * 0.1 + i * Math.PI * 2 / 3 - 1.15;
          const x = 2680 + Math.cos(angle) * 645, y = 1100 + Math.sin(angle) * 465;
          return <g key={tool} opacity={easeIn(time, 0.65 + i * 0.55)} transform={`translate(${x} ${y})`}>
            <rect x="-165" y="-48" width="330" height="96" rx="35" fill="white" stroke="#b9c8be" strokeWidth="3"/>
            <text textAnchor="middle" y="15" fontSize="39" fill={INK}>{tool}</text>
          </g>;
        })}
      </g>
      <g transform={`translate(2680 1100) scale(${0.4 + gather * 0.6})`} opacity={gather}>
        <circle r="225" fill="#e7f3ed"/>
        <path d="M-132 0L0 -116L132 0L0 116Z" fill="none" stroke="#627f72" strokeWidth="11" strokeDasharray="720" strokeDashoffset={720 * (1 - easeIn(time, 1.1, 1.5))}/>
        {[[-132,0],[0,-116],[132,0],[0,116]].map(([x,y], i) => <circle key={i} cx={x} cy={y} r={24 + Math.sin(time * 1.5 + i) * 2} fill={TEAL}/>)}
      </g>
      <text x="2680" y="1740" textAnchor="middle" fontSize="43" fontWeight="700" fill={TEAL} opacity={easeIn(time, 2.8)}>One graph. Every tool.</text>
    </svg>
  </>;
}

// Scene 2: the CLI types on the left, then sends a visible capture packet to the right.
function SceneCLI({time}) {
  const prompt = 'Reply in British English, and use em dashes.';
  const capture = easeIn(time, 12.1, 1.0);
  return <>
    <Heading time={time} at={7.1} title="Your prompt becomes useful context." subtitle="Captured before the agent starts work."/>
    <motion.div initial={false} style={{position: 'absolute', left: 280, top: 695, width: 1730, height: 960, borderRadius: 30, background: INK, color: '#f5f7ef', boxShadow: '0 24px 70px #20252218', x: -100 * (1 - easeIn(time, 7.25, 0.8)), opacity: easeIn(time, 7.25, 0.8)}}>
      <div style={{height: 115, borderBottom: '2px solid #4c574e', display: 'flex', alignItems: 'center', gap: 20, padding: '0 60px'}}>{['#a5b4a6','#859889','#657c6c'].map(c => <span key={c} style={{width: 20, height: 20, borderRadius: '50%', background: c}}/>)}<span style={{marginLeft: 40, fontSize: 36}}>Your running CLI</span></div>
      <div style={{padding: '85px 65px', fontFamily: 'monospace', fontSize: 57, lineHeight: 1.45}}><span style={{color: '#8fcbb3'}}>&gt; </span>{typed(prompt, time, 7.8, 4.0)}<span style={{color: '#8fcbb3', opacity: Math.floor(time * 2.7) % 2 ? 0 : 1}}>▏</span>
        <div style={{marginTop: 96, fontSize: 38, color: '#b2c1b5', opacity: easeIn(time, 12.0)}}>{time < 14.6 ? 'Capturing relevant preferences…' : 'Shared memory ready ✓'}</div>
        <div style={{fontSize: 33, color: '#8fcbb3', marginTop: 54, opacity: easeIn(time, 14.8)}}>The agent can start with your context.</div>
      </div>
    </motion.div>
    <div style={{position: 'absolute', left: 2460, top: 705, color: TEAL, fontSize: 46, fontWeight: 700, opacity: easeIn(time, 11.8)}}>Your shared memory</div>
    <svg width="3840" height="2160" viewBox="0 0 3840 2160" style={{position: 'absolute', inset: 0}}>
      <path d="M2020 1175H2420" stroke="#9abdae" strokeWidth="6" fill="none" opacity={capture}/>
      <path d="M2387 1150L2420 1175L2387 1200" stroke={TEAL} strokeWidth="6" fill="none" opacity={capture}/>
      <circle cx={mix(2020,2420,capture)} cy="1175" r="18" fill={TEAL} opacity={capture}/>
      <GraphNode x={mix(2250,3010,easeIn(time,12.8,0.8))} y={990} width={1050} height={260} label="British English" detail="Writing preference · From your prompt" opacity={easeIn(time,12.8,0.8)}/>
      <GraphNode x={mix(2250,3010,easeIn(time,14.0,0.8))} y={1350} width={1050} height={260} label="Use em dashes" detail="Writing preference · From your prompt" opacity={easeIn(time,14.0,0.8)}/>
      <text x="2460" y="1640" fontSize="35" fill="#707872" opacity={easeIn(time,15.0)}>Remembered once. Available across tools.</text>
    </svg>
  </>;
}

// Scene 3: the central graph assembles radially, with its prompt evidence kept separate.
function SceneAssembly({time}) {
  const english = easeIn(time, 17.45, 1.25), dash = easeIn(time, 18.05, 1.15);
  return <>
    <Heading time={time} at={17.05} title="Two preferences. One memory." subtitle="Only your prompts create memory nodes." center/>
    <svg width="3840" height="2160" viewBox="0 0 3840 2160" style={{position: 'absolute', inset: 0}}>
      <circle cx="1920" cy="1100" r={490 + Math.sin(time * 1.0) * 4} fill="none" stroke="#e1e8df" strokeWidth="3" strokeDasharray="8 24"/>
      <path d="M1920 1100Q1450 875 965 1100" fill="none" stroke="#8bb5a5" strokeWidth="6" pathLength="1" strokeDasharray="1" strokeDashoffset={1-english}/>
      <path d="M1920 1100Q2390 1325 2875 1100" fill="none" stroke="#8bb5a5" strokeWidth="6" pathLength="1" strokeDasharray="1" strokeDashoffset={1-dash}/>
      <Packet a={[1920,1100]} b={[965,1100]} time={time} opacity={english}/><Packet a={[1920,1100]} b={[2875,1100]} time={time} phase={0.5} opacity={dash}/>
      <GraphNode x={mix(1920,965,english)} y={1100 - 130 * Math.sin(Math.PI * english)} label="British English" width={720} height={250} opacity={english} selected/>
      <GraphNode x={mix(1920,2875,dash)} y={1100 + 130 * Math.sin(Math.PI * dash)} label="Use em dashes" width={720} height={250} opacity={dash}/>
      <g opacity={easeIn(time,17.2)}><MemoryCore x={1920} y={1100} radius={180} time={time}/></g>
      <g opacity={easeIn(time,19.7,0.65)} transform={`translate(0 ${45*(1-easeIn(time,19.7,0.65))})`}>
        <path d="M1920 1320V1470" fill="none" stroke="#adbdaf" strokeWidth="4" strokeDasharray="9 12"/>
        <rect x="760" y="1470" width="2320" height="215" rx="28" fill="#edf4ec" stroke="#d7e3d7" strokeWidth="3"/>
        <text x="1920" y="1535" textAnchor="middle" fontSize="30" fill={TEAL}>Prompt evidence</text>
        <text x="1920" y="1610" textAnchor="middle" fontSize="44" fill={INK}>“Reply in British English, and use em dashes.”</text>
      </g>
      <text x="1920" y="1740" textAnchor="middle" fontSize="35" fill="#707872" opacity={easeIn(time,21.2)}>Agent replies stay outside memory creation.</text>
    </svg>
  </>;
}

// Scene 4: a targeted correction dissolves one node, then the surviving graph recentres.
function SceneCorrection({time}) {
  const removed = easeIn(time,32.0,0.95), recover = easeIn(time,32.7,1.4);
  const rootX = mix(1640,2260,recover), englishX = mix(740,1360,recover);
  const cursorMove = easeIn(time,30.0,0.8);
  return <>
    <Heading time={time} at={26.1} title="Change your mind. Change the memory." subtitle="Your correction applies across your chats and tools."/>
    <motion.div initial={false} style={{...panel, position: 'absolute', left: 800, top: 665, width: 2260, height: 275, padding: '44px 56px', opacity: easeIn(time,26.5), scale: 0.94 + easeIn(time,26.5)*0.06}}>
      <div style={{fontSize: 30, color: '#707872', marginBottom: 24}}>Say the change in plain English</div>
      <div style={{fontSize: 48, width: 1820, lineHeight: 1.25}}>{typed('Forget em dashes. That was just for this task.',time,27.0,2.5)}<span style={{color: TEAL, opacity: time<29.6 && Math.floor(time*2.6)%2===0 ? 1 : 0}}>▏</span></div>
      <div style={{position: 'absolute', right: 48, top: 122, width: 96, height: 96, borderRadius: '50%', background: TEAL, color: 'white', display: 'grid', placeItems: 'center', fontSize: 55}}>↑</div>
    </motion.div>
    <svg width="3840" height="2160" viewBox="0 0 3840 2160" style={{position: 'absolute', inset: 0}}>
      <path d={`M${rootX} 1260L${englishX} 1260`} stroke="#8bb5a5" strokeWidth="6"/>
      <path d={`M${rootX} 1260L${mix(2770,rootX,removed)} 1260`} stroke={time>=30.5 ? '#b5aaa0' : '#8bb5a5'} strokeWidth="6" strokeDasharray={time>=30.5 ? '12 15' : undefined} opacity={1-removed}/>
      <Packet a={[rootX,1260]} b={[englishX,1260]} time={time}/>
      <GraphNode x={englishX} y={1260} width={710} height={250} label="British English" detail={recover>0.5 ? 'Still active · Across all your chats' : 'From your prompt'} selected={recover>0.3}/>
      <g transform={`translate(${removed*90} ${removed*90}) rotate(${removed*7} 2770 1260)`}>
        <GraphNode x={2770} y={1260} width={710} height={250} label="Use em dashes" opacity={1-removed} scale={1-removed*0.25} selected={time>=30.4} color="#ad8875"/>
      </g>
      <MemoryCore x={rootX} y={1260} time={time} radius={165}/>
      {time>32.3 && <g opacity={easeIn(time,32.3)* (1-easeIn(time,34.0))}><circle cx="2820" cy="1270" r="60" fill="#e8f3e8"/><text x="2820" y="1292" textAnchor="middle" fontSize="55" fill={TEAL}>✓</text></g>}
      <text x="1920" y="1690" textAnchor="middle" fontSize="51" fontWeight="700" fill={TEAL} opacity={easeIn(time,33.5)}>British English stays. Everywhere.</text>
      {['CLI','Coding agent','Editor'].map((label,i)=><g key={label} opacity={easeIn(time,34.2+i*0.4)}><text x={1300+i*620} y="1750" textAnchor="middle" fontSize="33" fill="#707872">{label} ✓</text></g>)}
    </svg>
    {time>=28.9 && time<33 && <Cursor x={mix(2940,2760,cursorMove)} y={mix(800,1210,cursorMove)} time={time} clickAt={time<30.2 ? 29.8 : 31.0} opacity={1-easeIn(time,32.4,0.6)}/>}
  </>;
}

// Scene 5: memory search, repository graph, and agent work each have their own visual grammar.
function SceneRetrieval({time}) {
  const memory = easeIn(time,37.4), code = easeIn(time,39.8), work = easeIn(time,42.2);
  const ring = 2*Math.PI*240;
  return <>
    <Heading time={time} at={37.1} title="Memory first. Then the existing code graph." subtitle="Two separate stores, consulted in order."/>
    <svg width="3840" height="2160" viewBox="0 0 3840 2160" style={{position: 'absolute', inset: 0}}>
      <g opacity={memory}>
        <text x="750" y="710" textAnchor="middle" fontSize="43" fontWeight="700" fill={TEAL}>1 · Your memory</text>
        <circle cx="750" cy="1130" r="240" fill="none" stroke="#dfebe0" strokeWidth="18"/>
        <circle cx="750" cy="1130" r="240" fill="none" stroke={TEAL} strokeWidth="18" strokeDasharray={ring} strokeDashoffset={ring*(1-easeIn(time,37.5,1.8))} transform="rotate(-90 750 1130)"/>
        <circle cx={750+Math.cos(time*0.8)*240} cy={1130+Math.sin(time*0.8)*240} r="19" fill={TEAL}/>
        <MemoryCore x={750} y={1130} radius={153} time={time} label="Memory" sub="Personal context"/>
        <rect x="370" y="1480" width="760" height="125" rx="28" fill="#e7f3eb"/><text x="750" y="1558" textAnchor="middle" fontSize="40" fill={TEAL}>British English ✓</text>
      </g>
      <g opacity={code} transform={`translate(0 ${-50*(1-code)})`}>
        <text x="1920" y="710" textAnchor="middle" fontSize="43" fontWeight="700" fill={BLUE}>2 · Existing code graph</text>
        <rect x="1460" y="790" width="920" height="720" rx="28" fill="#edf0f5" stroke="#d1d8e4" strokeWidth="3"/>
        <path d="M1550 900H2290M1550 1440H2290" stroke="#d0d8e4" strokeWidth="3"/>
        <text x="1560" y="866" fontSize="30" fill={BLUE}>Repository context</text>
        {[[1650,1040,'module'],[2180,1110,'function'],[1900,1340,'test']].map(([x,y,label],i)=><g key={label} opacity={easeIn(time,40.0+i*0.3)}>
          {i>0 && <path d={i===1 ? 'M1650 1040L2180 1110' : 'M2180 1110L1900 1340'} stroke="#9aabc9" strokeWidth="5" pathLength="1" strokeDasharray="1" strokeDashoffset={1-easeIn(time,40.1+i*0.3)}/>}
          <rect x={x-130} y={y-58} width="260" height="116" rx="18" fill="white" stroke={BLUE} strokeWidth="3"/>
          <text x={x} y={y+14} textAnchor="middle" fontSize="35" fill={BLUE}>{label}</text>
        </g>)}
        <text x="1920" y="1580" textAnchor="middle" fontSize="34" fill={BLUE}>Original graph kept intact ✓</text>
      </g>
      <g opacity={work} transform={`translate(${70*(1-work)} 0)`}>
        <text x="3090" y="710" textAnchor="middle" fontSize="43" fontWeight="700" fill={INK}>3 · Agent work</text>
        <rect x="2670" y="850" width="840" height="710" rx="28" fill={INK}/>
        <text x="2740" y="955" fontSize="33" fill="#becbbe">Context ready</text>
        <path d="M2740 990H3440" stroke="#4d5b4f" strokeWidth="3"/>
        {['Read relevant context','Plan the change','Start the work'].map((label,i)=><g key={label} opacity={easeIn(time,42.7+i*0.85)} transform={`translate(${25*(1-easeIn(time,42.7+i*0.85))} 0)`}>
          <circle cx="2770" cy={1090+i*150} r="25" fill="#87bea2"/><text x="2770" y={1101+i*150} textAnchor="middle" fontSize="30" fill={INK}>✓</text><text x="2830" y={1104+i*150} fontSize="36" fill="white">{label}</text>
        </g>)}
      </g>
      {[[1145,1400,39.35],[2390,2640,41.8]].map(([x1,x2,at],i)=><g key={i} opacity={easeIn(time,at)}>
        <path d={`M${x1} 1170H${x2}`} stroke="#aab9ac" strokeWidth="5" strokeDasharray="10 14"/>
        <path d={`M${x2-28} 1150L${x2} 1170L${x2-28} 1190`} fill="none" stroke="#859b8c" strokeWidth="5"/>
        <Packet a={[x1,1170]} b={[x2,1170]} time={time} phase={i*0.3} color={i===0 ? TEAL : BLUE}/>
      </g>)}
      <text x="1920" y="1740" textAnchor="middle" fontSize="36" fill="#707872" opacity={easeIn(time,44.8)}>Temporary retrieval flow. No permanent cross-graph edges.</text>
    </svg>
  </>;
}

// Scene 6: a wide dashboard pans to a new node and opens one inspector.
function SceneDashboard({time}) {
  const entry = easeIn(time,48.05,0.55), incoming = easeIn(time,49.25,0.85), friday = easeIn(time,50.7,0.75);
  const inspect = easeIn(time,51.65,0.65), pan = easeIn(time,51.45,0.9)*150;
  const nodes = [
    {id:'english',x:750,y:275,label:'British English',detail:'From your prompt',a:1},
    {id:'concise',x:mix(2710,2010,incoming),y:mix(110,735,incoming),label:'Prefer concise replies',detail:'From another chat',a:incoming},
    {id:'friday',x:mix(50,710,friday),y:mix(960,825,friday),label:'Deploy on Fridays',detail:'From your CLI prompt',a:friday},
  ];
  const selectedX=430+2010-pan, selectedY=110+735;
  return <>
    <Heading time={time} at={48.0} title="Your graph, across every chat." subtitle="Inspect and edit what your agent remembers."/>
    <motion.div initial={false} style={{...panel,position:'absolute',left:260,top:620,width:3320,height:1150,overflow:'hidden',opacity:entry,y:80*(1-entry)}}>
      <div style={{height:110,borderBottom:'2px solid #dce2dc',display:'flex',alignItems:'center',padding:'0 44px',gap:45}}><span style={{fontSize:34,color:'#707872'}}>Lovable</span><strong style={{fontSize:43}}>Agent memory</strong><span style={{marginLeft:'auto',fontSize:30,color:TEAL}}>One shared graph · {time<50.0 ? 1 : time<51.4 ? 2 : 3} memories</span></div>
      <div style={{position:'absolute',left:0,top:110,bottom:0,width:430,background:'#f1f4ee',borderRight:'2px solid #dce2dc',padding:'47px 40px'}}><div style={{fontSize:33,fontWeight:700,marginBottom:52}}>Sources</div>{['CLI','Coding agent','Editor'].map((label,i)=><div key={label} style={{fontSize:32,padding:'25px 24px',marginBottom:19,borderRadius:18,background:i===0 ? '#e0eee1' : 'transparent',color:i===0 ? TEAL : '#707872'}}><span style={{fontSize:24,marginRight:17}}>●</span>{label}</div>)}<div style={{position:'absolute',left:40,bottom:62,right:35,fontSize:29,color:'#707872',lineHeight:1.4}}>All sources contribute to the same memory.</div></div>
      <svg width="2890" height="1040" viewBox="0 0 2890 1040" style={{position:'absolute',left:430,top:110}}>
        <defs><pattern id="dashboard-grid" width="65" height="65" patternUnits="userSpaceOnUse"><circle cx="20" cy="20" r="2.5" fill="#dfe6df"/></pattern></defs><rect width="2890" height="1040" fill="url(#dashboard-grid)"/>
        <g transform={`translate(${-pan} 0)`}>
          {nodes.map((n,i)=><g key={n.id} opacity={n.a}>
            <path d={`M1320 520Q${(1320+n.x)/2} 520 ${n.x} ${n.y}`} fill="none" stroke="#92b7a7" strokeWidth="5"/>
            <Packet a={[1320,520]} b={[n.x,n.y]} time={time} phase={i*0.3}/>
            <GraphNode x={n.x} y={n.y+Math.sin(time+i)*4} label={n.label} detail={n.detail} width={630} height={205} selected={n.id==='concise' && time>=51.4}/>
          </g>)}
          <MemoryCore x={1320} y={520} radius={165} time={time}/>
        </g>
        {time<50.7 && <g opacity={easeIn(time,48.7)* (1-easeIn(time,50.15))}><rect x="1930" y="45" width="770" height="115" rx="26" fill="#e5f2e8"/><text x="2315" y="92" textAnchor="middle" fontSize="27" fill={TEAL}>New prompt from another chat</text><text x="2315" y="136" textAnchor="middle" fontSize="34" fill={INK}>“I prefer concise replies.”</text></g>}
      </svg>
      <motion.div initial={false} style={{position:'absolute',left:2600+720*(1-inspect),top:110,bottom:0,width:720,padding:'53px 48px',background:'#fbfcf8',borderLeft:'3px solid #d9e4d9',opacity:inspect}}>
        <div style={{fontSize:29,color:TEAL}}>Selected memory</div><div style={{fontSize:51,lineHeight:1.15,fontWeight:700,marginTop:32}}>Prefer concise replies</div><div style={{fontSize:30,color:'#707872',marginTop:35}}>Applies across all your chats</div>
        <div style={{height:2,background:'#e0e7df',margin:'50px 0'}}/><div style={{fontSize:31,fontWeight:700}}>Prompt evidence</div><div style={{fontSize:35,lineHeight:1.35,marginTop:28}}>“Remember: I prefer concise replies.”</div><div style={{fontSize:29,color:'#707872',marginTop:34}}>Source: your prompt</div>
        <div style={{display:'flex',gap:22,marginTop:60}}>{['Edit','Forget'].map((label,i)=><div key={label} style={{width:280,padding:'24px 0',borderRadius:19,fontSize:33,textAlign:'center',border:`2px solid ${i===0 ? TEAL : '#c9d4c8'}`,color:i===0 ? TEAL : '#707872',background:i===0 ? '#e7f2e8' : 'white'}}>{label}</div>)}</div>
      </motion.div>
      {time>=50.8 && <Cursor x={mix(3020,selectedX+160,easeIn(time,50.8,0.65))} y={mix(370,selectedY-70,easeIn(time,50.8,0.65))} time={time} clickAt={51.5}/>}
    </motion.div>
  </>;
}

// Scene 7: memories gather into the four-node symbol, followed by the centred brand reveal.
function SceneOutro({time}) {
  const gather=easeIn(time,56.15,1.1), brand=easeIn(time,57.0,0.75);
  const starts=[[1050,710],[2880,790],[2480,1540],[1260,1480]];
  const ends=[[1740,930],[1920,770],[2100,930],[1920,1090]];
  return <>
    <svg width="3840" height="2160" viewBox="0 0 3840 2160" style={{position:'absolute',inset:0}}>
      <g opacity={1-gather}>{starts.map(([x,y],i)=><g key={i}><path d={`M1920 1030Q${x} 1030 ${x} ${y}`} fill="none" stroke="#bdd3c3" strokeWidth="4"/><text x={x} y={y+85} textAnchor="middle" fontSize="35" fill="#707872">{['British English','Concise replies','CLI prompt','Another chat'][i]}</text></g>)}</g>
      <circle cx="1920" cy="930" r={mix(440,260,gather)+Math.sin(time*1.4)*3} fill="#edf4ed" opacity={0.7*brand}/>
      <path d="M1740 930L1920 770L2100 930L1920 1090Z" fill="none" stroke="#627f72" strokeWidth="11" pathLength="1" strokeDasharray="1" strokeDashoffset={1-easeIn(time,56.9,0.55)}/>
      {starts.map(([x,y],i)=>{
        const tx=mix(x,ends[i][0],gather),ty=mix(y,ends[i][1],gather)-Math.sin(Math.PI*gather)*120*(i%2 ? -1 : 1);
        return <circle key={i} cx={tx} cy={ty} r={mix(44,26,gather)+Math.sin(time*1.6+i)*1.5} fill={TEAL}/>;
      })}
    </svg>
    <motion.div initial={false} style={{position:'absolute',left:300,top:1270,width:3240,textAlign:'center',opacity:brand,scale:0.97+brand*0.03,clipPath:`inset(0 ${100*(1-brand)}% 0 0)`}}><div style={{fontSize:110,fontWeight:700,letterSpacing:-3}}>code-review-graph</div><div style={{fontSize:61,marginTop:58,color:TEAL}}>Your memory. Your control.</div></motion.div>
    <div style={{position:'absolute',left:500,top:1780,width:2840,textAlign:'center',fontSize:29,color:'#707872',opacity:easeIn(time,58.2)}}>Illustrative product demo · AI narration</div>
  </>;
}

function MovingProduct({time, scene}) {
  const scenes=[SceneIntro,SceneCLI,SceneAssembly,SceneCorrection,SceneRetrieval,SceneDashboard,SceneOutro];
  const Scene=scenes[scene];
  return <Scene time={time}/>;
}

export function AgentMemoryFilm({captions = []}) {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const time = frame / fps;
  const camera = cameraAt(time);
  const scene = Math.max(0, TIMES.findIndex((start, i) => i < 7 && time >= start && time < TIMES[i + 1]));
  const cue = captions.find((item) => time >= item.start && time < item.end);
  const [fontHandle] = useState(() => delayRender('Load local DM Sans'));
  useEffect(() => {
    Promise.all([document.fonts.load('400 64px "Film Sans"'), document.fonts.load('700 66px "Film Sans"')])
      .then(() => continueRender(fontHandle))
      .catch(() => continueRender(fontHandle));
  }, [fontHandle]);

  return <AbsoluteFill style={{background: BG, overflow: 'hidden', fontFamily: '"Film Sans", Arial, sans-serif'}}>
    <style>{`@font-face {font-family: 'Film Sans'; src: url('${staticFile('DMSans-Regular.ttf')}'); font-weight: 400;} @font-face {font-family: 'Film Sans'; src: url('${staticFile('DMSans-Bold.ttf')}'); font-weight: 700;}`}</style>
    <motion.div initial={false} style={{
      position: 'absolute', inset: 0,
      scale: camera.scale, x: camera.x, y: camera.y,
      transformOrigin: `${camera.ox * 100}% ${camera.oy * 100}%`,
      willChange: 'transform',
    }}>
      <MovingProduct time={time} scene={scene}/>
    </motion.div>

    {/* A steady frame protects the brand, captions and progress from camera motion. */}
    <div style={{position: 'absolute', top: 0, left: 0, right: 0, height: 246, background: BG}} />
    <div style={{position: 'absolute', top: 72, left: 200, display: 'flex', alignItems: 'center', gap: 28, color: INK}}>
      <svg width="91" height="80" viewBox="0 0 91 80"><path d="M9 40L44 10L78 40L44 69Z" fill="none" stroke="#707872" strokeWidth="5" />{[[9,40],[44,10],[78,40],[44,69]].map(([cx,cy])=><circle key={`${cx}:${cy}`} cx={cx} cy={cy} r="8" fill={TEAL}/>)}</svg>
      <span style={{fontSize: 64, fontWeight: 700, letterSpacing: -1.2}}>code-review-graph</span>
      <span style={{fontSize: 34, color: '#707872', marginLeft: 10, paddingTop: 6}}>Agent memory</span>
    </div>
    <div style={{position: 'absolute', top: 210, left: 200, fontSize: 36, color: '#707872'}}>{String(scene + 1).padStart(2, '0')} / 07</div>
    <div style={{position: 'absolute', right: 200, top: 84, padding: '16px 35px', borderRadius: 35, background: '#edeee8', color: '#707872', fontSize: 42}}>Illustrative demo</div>
    <div style={{position: 'absolute', top: 202, left: 200, right: 200, height: 3, background: '#dce2dc'}} />
    <Caption cue={cue} time={time} />
    <div style={{position: 'absolute', bottom: 0, height: 10, left: 0, right: 0, background: '#e2e7df'}}>
      <motion.div initial={false} style={{width: `${100 * time / 60}%`, height: '100%', background: TEAL}} />
    </div>
    <Audio src={staticFile('voiceover.wav')} />
  </AbsoluteFill>;
}

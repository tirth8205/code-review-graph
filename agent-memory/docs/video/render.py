#!/usr/bin/env python3
"""Render a deterministic 60-second illustrated code-review-graph explainer.

Requires Pillow and ffmpeg. All artwork is drawn locally; no credentials or
screenshots are read. Re-render with the bundled Python and --render.
"""
from pathlib import Path
from functools import lru_cache
import math, subprocess, json, argparse, re
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
W, H, FPS, DURATION = 1920, 1080, 24, 60
SCALE = 2
PIX_W, PIX_H = W*SCALE, H*SCALE
BG = '#f7f7f3'
INK = '#202522'
MUTED = '#707872'
LINE = '#dce2dc'
TEAL = '#087f71'
TEAL_LIGHT = '#e9f3ef'
BLUE = '#416593'
BLUE_LIGHT = '#edf1f8'
RED = '#a25e54'
REG = '/Users/tirth/Library/Fonts/DMSans-Regular.ttf'
BOLD = '/Users/tirth/Library/Fonts/DMSans-Bold.ttf'
if not Path(REG).exists():
    REG, BOLD = '/System/Library/Fonts/Supplemental/Arial.ttf', '/System/Library/Fonts/Supplemental/Arial Bold.ttf'
TIMES = [0, 7, 17, 26, 37, 48, 56, 60]

@lru_cache(None)
def font(size, bold=False):
    return ImageFont.truetype(BOLD if bold else REG, size*SCALE)

class ScreenDraw:
    """Draw logical coordinates at native 4K resolution, including font glyphs."""
    def __init__(self, im): self.d = ImageDraw.Draw(im)
    @staticmethod
    def coords(xy):
        if isinstance(xy[0], (tuple,list)):
            return [(x*SCALE,y*SCALE) for x,y in xy]
        return tuple(x*SCALE for x in xy)
    def text(self,xy,value,**kwargs): self.d.text(self.coords(xy),value,**kwargs)
    def textlength(self,value,**kwargs): return self.d.textlength(value,**kwargs)/SCALE
    def rounded_rectangle(self,xy,radius,**kwargs):
        if 'width' in kwargs: kwargs['width']*=SCALE
        self.d.rounded_rectangle(self.coords(xy),radius*SCALE,**kwargs)
    def rectangle(self,xy,**kwargs):
        if 'width' in kwargs: kwargs['width']*=SCALE
        self.d.rectangle(self.coords(xy),**kwargs)
    def ellipse(self,xy,**kwargs):
        if 'width' in kwargs: kwargs['width']*=SCALE
        self.d.ellipse(self.coords(xy),**kwargs)
    def line(self,xy,**kwargs):
        if 'width' in kwargs: kwargs['width']*=SCALE
        self.d.line(self.coords(xy),**kwargs)
    def polygon(self,xy,**kwargs): self.d.polygon(self.coords(xy),**kwargs)

def clamp(x): return max(0., min(1., x))
def ease(x): return 1 - (1-clamp(x))**3
def smooth(x):
    x = clamp(x)
    return x*x*(3-2*x)

def text(d, xy, value, size=36, fill=INK, bold=False, anchor=None):
    d.text(xy, value, font=font(size, bold), fill=fill, anchor=anchor)

def lines(d, xy, value, size=36, fill=INK, bold=False, spacing=1.28):
    x, y = xy
    for item in value.split('\n'):
        text(d, (x,y), item, size, fill, bold)
        y += size*spacing

def roundbox(d, box, fill='white', stroke=LINE, r=28, width=2, shadow=False):
    if shadow:
        x1,y1,x2,y2=box
        d.rounded_rectangle((x1,y1+7,x2,y2+7),r,fill='#eeefea')
    d.rounded_rectangle(box,r,fill=fill,outline=stroke,width=width)

def pill(d, xy, value, color=TEAL, fill=TEAL_LIGHT, size=25):
    x,y=xy
    length=d.textlength(value,font=font(size))
    roundbox(d,(x,y,x+length+38,y+size+23),fill,fill,r=18,width=1)
    text(d,(x+19,y+8),value,size,color)

def arrow(d, start, end, p=1, color=TEAL, width=5, moving=False):
    x1,y1=start; x2,y2=end
    xe=x1+(x2-x1)*ease(p); ye=y1+(y2-y1)*ease(p)
    d.line((x1,y1,xe,ye),fill=color,width=width)
    if p>.95:
        angle=math.atan2(y2-y1,x2-x1)
        pts=[(x2,y2),(x2-17*math.cos(angle-.5),y2-17*math.sin(angle-.5)),(x2-17*math.cos(angle+.5),y2-17*math.sin(angle+.5))]
        d.polygon(pts,fill=color)
    if moving:
        z=(p%1)
        xx=x1+(x2-x1)*z; yy=y1+(y2-y1)*z
        d.ellipse((xx-9,yy-9,xx+9,yy+9),fill=color)

def circle(d, xy, radius=12, fill=TEAL):
    x,y=xy
    d.ellipse((x-radius,y-radius,x+radius,y+radius),fill=fill)

def node(d, box, title, detail='', color=TEAL, active=True, grow=1, tag=None):
    if grow<=0: return
    x1,y1,x2,y2=box
    shift=22*(1-ease(grow))
    y1+=shift; y2+=shift
    roundbox(d,(x1,y1,x2,y2), 'white' if active else '#f1f2ee', color if active else LINE, r=22,width=2,shadow=active)
    circle(d,(x1+29,y1+32),7,color if active else MUTED)
    text(d,(x1+48,y1+14),tag or ('CONSTRAINT' if active else 'HISTORY'),22,color if active else MUTED)
    lines(d,(x1+27,y1+62),title,32,INK if active else MUTED,bold=True,spacing=1.12)
    if detail: text(d,(x1+27,y2-43),detail,22,MUTED)

def title(d, eyebrow, heading, subtitle='', local=0):
    shift=14*(1-ease(local/.65))
    text(d,(100,165+shift),eyebrow,25,TEAL,True)
    text(d,(100,212+shift),heading,68,INK,True)
    if subtitle: text(d,(100,301+shift),subtitle,32,MUTED)

def base(index):
    im=Image.new('RGB',(PIX_W,PIX_H),BG); d=ScreenDraw(im)
    # Compact graph mark, not a third-party brand mark.
    d.line((100,63,122,46,144,66,122,83,100,63),fill=MUTED,width=3)
    for p in [(100,63),(122,46),(144,66),(122,83)]: circle(d,p,5,TEAL)
    text(d,(162,43),'code-review-graph',33,INK,True)
    text(d,(100,110),f'{index+1:02} / 07',20,MUTED)
    pill(d,(1570,44),'Illustrative demo',MUTED,'#edeee8',22)
    d.line((100,101,1820,101),fill=LINE,width=2)
    return im,d

def opener(d,t):
    text(d,(100,278),'Your agent’s memory.',84,INK,True)
    lines(d,(100,393),'Visible.\nEditable.\nYours.',90,INK,True,1.16)
    text(d,(102,790),'A conversation graph you can control.',32,MUTED)
    pts=[(1340,492),(1580,333),(1700,550),(1460,730),(1140,720),(1120,361)]
    for i,p in enumerate(pts):
        q=pts[(i+1)%len(pts)]
        d.line((*p,*q),fill=LINE,width=3)
        d.line((*p,1340,492),fill='#bdcec5',width=3)
    for i,p in enumerate(pts):
        a=ease((t-i*.18)/1.2)
        circle(d,p,10+7*a,TEAL if i%2 else BLUE)
    r=55+3*math.sin(t*1.8)
    circle(d,(1340,492),r,TEAL)
    text(d,(1340,492),'M',55,'white',True,'mm')
    pill(d,(1450,270),'Preferences')
    pill(d,(1400,782),'Decisions')
    pill(d,(1050,298),'Facts')

def prompt(d,t):
    title(d,'01 · USER PROMPT','Say it once. Keep what matters.',local=t)
    roundbox(d,(100,362,1120,790),'white',LINE,28,shadow=True)
    d.line((100,430,1120,430),fill=LINE,width=2)
    for x,c in [(130,'#dcded8'),(155,'#dcded8'),(180,'#dcded8')]: circle(d,(x,398),6,c)
    text(d,(220,380),'Connected coding tool',25,MUTED)
    text(d,(140,465),'YOU',23,TEAL,True)
    message='Implement refund validation.\nWhen replying, use British English\nand em dashes.'
    n=int(len(message)*ease((t-.7)/4.3))
    shown=message[:max(0,n)]
    lines(d,(140,515),shown,42,INK,False,1.25)
    if t<5.2:
        last=shown.split('\n')[-1]
        yy=515+(len(shown.split('\n'))-1)*52.5
        xx=140+d.textlength(last,font=font(42))
        if int(t*3)%2==0: d.line((xx+4,yy+8,xx+4,yy+46),fill=TEAL,width=3)
    roundbox(d,(1370,465,1815,715),TEAL_LIGHT,'#bccec4',24,shadow=True)
    text(d,(1410,498),'MEMORY CAPTURE',23,TEAL,True)
    lines(d,(1410,550),'User prompt\nonly',42,INK,True,1.12)
    arrow(d,(1150,592),(1333,592),clamp((t-4.5)/1.0))
    if t>5.5:
        pill(d,(1390,748),'Before agent work',TEAL,TEAL_LIGHT,24)
    text(d,(140,827),'The assistant’s reply is work output.',30,MUTED)

def preferences(d,t):
    title(d,'02 · CHAT MEMORY','Two preferences. Two memories.',local=t)
    centre=(960,566)
    a=ease(t/1.2); b=ease((t-.8)/1.2)
    d.line((620,543,*centre),fill='#b5cec4',width=5)
    d.line((*centre,1300,543),fill='#b5cec4',width=5)
    circle(d,centre,56,TEAL)
    text(d,(960,566),'Memory',22,'white',True,'mm')
    node(d,(140,440,640,655),'Reply in British English','Source: your user prompt',grow=a)
    node(d,(1280,440,1780,655),'Use em dashes','Source: your user prompt',grow=b)
    pill(d,(775,706),'Your memory graph',TEAL,TEAL_LIGHT,29)
    text(d,(960,841),'Assistant responses never become memory.',32,MUTED,anchor='mm')

def edit(d,t):
    title(d,'03 · HUMAN CONTROL','Keep the preference. Forget the rest.',local=t)
    roundbox(d,(100,355,1820,488),'white',LINE,22)
    text(d,(136,374),'MEMORY COMMAND',20,TEAL,True)
    text(d,(136,413),'“Forget the em-dash instruction. That was only for this task.”',37,INK)
    p=clamp((t-3.2)/2.4)
    d.line((620,667,960,667),fill='#b5cec4',width=4)
    if p<.9: d.line((960,667,1300,667),fill=LINE,width=3)
    circle(d,(960,667),42,TEAL)
    text(d,(960,667),'Memory',18,'white',True,'mm')
    node(d,(140,555,640,761),'Reply in British English','Kept in active memory')
    node(d,(1280,555,1780,761),'Use em dashes','Active preference' if p<.5 else 'Forgotten · excluded from retrieval',active=p<.5,color=TEAL if p<.5 else MUTED)
    if p>.5:
        d.line((1320,657,1660,657),fill='#bac0ba',width=2)
    if t>5.8:
        pill(d,(480,814),'Next turn: British English',TEAL,TEAL_LIGHT,30)
        text(d,(1140,828),'History retained',26,MUTED)

def pipeline(d,t):
    title(d,'04 · TWO GRAPH LAYERS','Memory first. Code context second.',local=t)
    boxes=[(100,390,625,814),(745,390,1270,814),(1390,390,1820,814)]
    roundbox(d,boxes[0],'white','#c0d6cc',26,shadow=True)
    roundbox(d,boxes[1],'white','#c9d4e3',26,shadow=True)
    roundbox(d,boxes[2],'white',LINE,26,shadow=True)
    text(d,(134,421),'1 · AGENT MEMORY',23,TEAL,True)
    text(d,(778,421),'2 · EXISTING CODE GRAPH',23,BLUE,True)
    text(d,(1426,421),'3 · AGENT WORK',23,MUTED,True)
    text(d,(134,463),'Preferences + facts',30,INK,True)
    text(d,(778,463),'Files + functions',30,INK,True)
    lines(d,(1426,477),'Implement\nrefund validation',32,INK,True,1.18)
    for a,b in [((235,601),(415,571)),((415,571),(489,670)),((235,601),(320,713))]:d.line((*a,*b),fill='#bad4c8',width=4)
    for p in [(235,601),(415,571),(489,670),(320,713)]:circle(d,p,13,TEAL)
    text(d,(136,759),'British English retained',24,TEAL)
    for a,b in [((835,630),(986,571)),((986,571),(1172,617)),((986,571),(1040,708)),((1040,708),(1169,731))]: d.line((*a,*b),fill='#c8d2df',width=4)
    for p in [(835,630),(986,571),(1172,617),(1040,708),(1169,731)]:circle(d,p,12,BLUE)
    text(d,(778,759),'Example: validate_refund',24,BLUE)
    arrow(d,(648,599),(721,599),clamp((t-1)/1.2),TEAL)
    arrow(d,(1293,599),(1366,599),clamp((t-4)/1.2),BLUE)
    if 1<t<5:
        x=650+clamp((t-1)/4)*67
        circle(d,(x,599),10,TEAL)
    if 4<t<8:
        x=1293+clamp((t-4)/4)*73
        circle(d,(x,599),10,BLUE)
    if t>5:
        pill(d,(1427,651),'Context prepared',TEAL,TEAL_LIGHT,24)
        text(d,(1427,727),'Then the agent acts.',27,MUTED)
    text(d,(960,862),'Separate stores · bounded context handoff · retrieval only',29,MUTED,anchor='mm')

def isolation(d,t):
    title(d,'05 · LOVABLE VIEW','One graph. Every new prompt.',local=t)
    roundbox(d,(100,352,1820,866),'white',LINE,26,shadow=True)
    d.rounded_rectangle((100,352,460,866),26,fill='#f0f2ed')
    d.rectangle((430,352,460,866),fill='#f0f2ed')
    text(d,(133,387),'Connected sources',26,INK,True)
    selected=0 if t<2.2 else (1 if t<5.0 else 2)
    for i,label in enumerate(['CLI','Editor','Chat']):
        y=454+i*74
        roundbox(d,(122,y,437,y+64),'white' if selected==i else '#f0f2ed',LINE if selected==i else '#f0f2ed',18)
        circle(d,(149,y+32),5,TEAL if selected==i else MUTED)
        text(d,(168,y+18),label,25,INK if selected==i else MUTED)
    text(d,(505,383),'Agent memory graph',31,INK,True)
    pill(d,(1540,385),'Memory only',MUTED,'#f0f2ed',23)
    d.line((910,575,1040,600),fill='#b5cec4',width=4)
    circle(d,(1040,600),43,TEAL)
    text(d,(1040,600),'Memory',19,'white',True,'mm')
    node(d,(520,480,915,674),'Reply in\nBritish English','Source: CLI prompt')
    if t>2.2:
        d.line((1040,600,1270,575),fill='#b5cec4',width=4)
        node(d,(1270,480,1730,674),'Maya owns billing','Source: editor prompt',grow=ease((t-2.2)/.75),tag='FACT')
    if t>5.0:
        d.line((1040,600,1165,680),fill='#b5cec4',width=4)
        node(d,(925,680,1410,833),'Refund window: 14 days','Source: chat prompt',grow=ease((t-5.0)/.75))
    text(d,(134,730),'1 memory graph',29,INK,True)
    text(d,(134,776),'More nodes',26,MUTED)
    text(d,(510,823),'Code graph stays separate.',23,MUTED)

def closing(d,t):
    text(d,(960,325),'code-review-graph',110,INK,True,'mm')
    lines(d,(410,462),'Memory you can see.\nMemory you can change.',72,INK,True,1.2)
    pill(d,(560,712),'Prompt → memory → code context → work',TEAL,TEAL_LIGHT,31)
    text(d,(960,851),'Inspect. Correct. Control.',32,MUTED,anchor='mm')

SCENES=[opener,prompt,preferences,edit,pipeline,isolation,closing]

@lru_cache(None)
def caption_events():
    path=ROOT/'voiceover.srt'
    if not path.exists(): return []
    def seconds(x):
        h,m,s=x.replace(',', '.').split(':')
        return int(h)*3600+int(m)*60+float(s)
    events=[]
    for block in path.read_text().strip().split('\n\n'):
        p=block.splitlines()
        if len(p)<3:continue
        start,end=p[1].split(' --> ')
        events.append((seconds(start),seconds(end),'\n'.join(p[2:])))
    return events

def wrap(d,value,size,width):
    result=[]; line=''
    for word in value.split():
        proposal=(line+' '+word).strip()
        if line and d.textlength(proposal,font=font(size))>width:
            result.append(line);line=word
        else:line=proposal
    if line:result.append(line)
    return result

def scene_image(index,t):
    im,d=base(index)
    SCENES[index](d,t-TIMES[index])
    return im

def frame(t,subtitles=True):
    idx=next(i for i in range(7) if TIMES[i]<=t<TIMES[i+1]) if t<60 else 6
    im=scene_image(idx,t)
    if idx and t<TIMES[idx]+.32:
        previous=scene_image(idx-1,TIMES[idx]-.001)
        im=Image.blend(previous,im,smooth((t-TIMES[idx])/.32))
    d=ScreenDraw(im)
    if subtitles:
        cap=next((value for a,b,value in caption_events() if a<=t<b),'')
        if cap:
            caplines=wrap(d,cap,33,1610)
            if len(caplines)>2: raise RuntimeError('Caption longer than two lines')
            top=951 if len(caplines)==1 else 924
            roundbox(d,(115,top-9,1805,1030),'#ecefe8','#ecefe8',18)
            for i,line in enumerate(caplines):
                text(d,(960,top+10+i*40),line,33,INK,anchor='mt')
    d.rectangle((0,H-5,W,H),fill='#e2e7df')
    d.rectangle((0,H-5,int(W*t/60),H),fill=TEAL)
    return im

def contact():
    stamps=[3.5,13,22,33,43,53,58]
    sheet=Image.new('RGB',(1280,360*4),'#e7eae3')
    for i,t in enumerate(stamps):
        small=frame(t).resize((640,360),Image.Resampling.LANCZOS)
        sheet.paste(small,((i%2)*640,(i//2)*360))
    sheet.save(ROOT/'storyboard.jpg',quality=94)
    frame(22).save(ROOT/'poster.jpg',quality=94)
    print('Storyboard and poster ready',flush=True)

def render(clean=False):
    video=ROOT/('memory-lens-base-4k.mp4' if clean else 'memory-lens-60s-4k.mp4')
    args=['ffmpeg','-hide_banner','-loglevel','error','-y','-f','rawvideo','-pixel_format','rgb24','-video_size',f'{PIX_W}x{PIX_H}','-framerate',str(FPS),'-i','pipe:0','-i',str(ROOT/'voiceover.wav'),'-t','60','-c:v','libx264','-preset','veryfast','-crf','18','-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart','-metadata','title=code-review-graph: memory you can inspect and control','-metadata','comment=Illustrative demo; no live agent or paid-provider footage',str(video)]
    process=subprocess.Popen(args,stdin=subprocess.PIPE)
    try:
        for i in range(FPS*DURATION):
            process.stdin.write(frame(i/FPS,subtitles=not clean).tobytes())
            if i% (FPS*10)==0: print(f'Rendered {i//FPS}/60 seconds',flush=True)
        process.stdin.close()
        if process.wait()!=0: raise RuntimeError('ffmpeg failed')
    except BaseException:
        process.kill();process.wait();raise
    print(f'Saved {video}',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--render',action='store_true');p.add_argument('--frame',type=float);p.add_argument('--clean',action='store_true')
    args=p.parse_args()
    if args.frame is not None:
        frame(args.frame).save(ROOT/f'frame-{args.frame:g}.jpg',quality=94)
    else:
        contact()
        if args.render:render(args.clean)

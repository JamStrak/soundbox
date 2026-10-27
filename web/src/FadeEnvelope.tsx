import {useEffect, useId, useMemo, useRef, useState} from 'react';
import type {KeyboardEvent, PointerEvent} from 'react';
import type {FadeCurve} from './api';
import {FADE_CURVES, fadeCurvePath} from './fadeCurves';
import './fade-envelope.css';

type FadeValues = {fade_in:number;fade_out:number};
type Props = {
  index:number;
  waveform:number[];
  sourceDuration:number;
  start:number;
  end:number;
  fadeIn:number;
  fadeOut:number;
  fadeInCurve:FadeCurve;
  fadeOutCurve:FadeCurve;
  disabled:boolean;
  onChange:(values:FadeValues)=>void;
  onCurveChange:(side:'in'|'out',curve:FadeCurve)=>void;
};
type Side = 'in'|'out';
type Drag = {side:Side;pointerId:number;button:HTMLButtonElement;grabOffset:number;startX:number;moved:boolean};
const finite=(value:number)=>Number.isFinite(value)?value:0;
const clamp=(value:number,min:number,max:number)=>Math.max(min,Math.min(max,value));
const seconds=(value:number)=>`${Number(finite(value).toFixed(3))} 秒`;
const navigationKeys=new Set(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End','PageUp','PageDown']);

export default function FadeEnvelope({index,waveform,sourceDuration,start,end,fadeIn,fadeOut,fadeInCurve,fadeOutCurve,disabled,onChange,onCurveChange}:Props) {
  const curveGroupId=useId();
  const trackRef=useRef<HTMLDivElement>(null);
  const dragRef=useRef<Drag|null>(null);
  const [dragging,setDragging]=useState<Side|null>(null);
  const length=Math.max(0,finite(end)-finite(start));
  const unavailable=disabled||length<=0;
  const valuesRef=useRef({length,fadeIn,fadeOut,unavailable,onChange});
  valuesRef.current={length,fadeIn,fadeOut,unavailable,onChange};
  const inRatio=length>0?clamp(finite(fadeIn)/length,0,1):0;
  const outRatio=length>0?1-clamp(finite(fadeOut)/length,0,1):1;
  const inX=inRatio*1000;
  const outX=outRatio*1000;
  const inPath=fadeIn>0?fadeCurvePath(fadeInCurve,'in',0,0,inX,100):'M0 0';
  const outPath=fadeOut>0?fadeCurvePath(fadeOutCurve,'out',outX,0,1000-outX,100):'M1000 0';
  const envelopePath=`${inPath}H${outX}${fadeOut>0?outPath.replace(/^M/,'L'):''}`;

  // These are the existing peak bins intersecting the crop, not new audio samples.
  const bars=useMemo(()=>{
    if(!waveform?.length||sourceDuration<=0||length<=0)return [];
    const first=clamp(Math.floor(start/sourceDuration*waveform.length),0,waveform.length-1);
    const last=clamp(Math.ceil(end/sourceDuration*waveform.length),first+1,waveform.length);
    const peaks=waveform.slice(first,last).map(value=>Math.abs(finite(value)));
    const maximum=Math.max(...peaks,.001);
    return peaks.map((peak,offset)=>{
      const sourceStart=(first+offset)/waveform.length*sourceDuration;
      const sourceEnd=(first+offset+1)/waveform.length*sourceDuration;
      const x=clamp((sourceStart-start)/length,0,1)*1000;
      const right=clamp((sourceEnd-start)/length,0,1)*1000;
      return {x,width:Math.max(0,right-x),height:peak/maximum*60};
    });
  },[waveform,sourceDuration,start,end,length]);

  function releaseDrag(){
    const drag=dragRef.current;dragRef.current=null;
    if(drag?.button.hasPointerCapture(drag.pointerId))drag.button.releasePointerCapture(drag.pointerId);
  }
  useEffect(()=>{
    if(unavailable){releaseDrag();setDragging(null);}
  },[unavailable]);
  useEffect(()=>()=>{releaseDrag();},[]);

  function change(side:Side,value:number){
    const latest=valuesRef.current;
    if(latest.unavailable||!Number.isFinite(value))return;
    const other=side==='in'?latest.fadeOut:latest.fadeIn;
    const maximum=Math.max(0,latest.length-other);
    // Round the requested value first, then clamp: rounding must not exceed the crop.
    const next=clamp(Math.round(value*1000)/1000,0,maximum);
    latest.onChange(side==='in'?{fade_in:next,fade_out:other}:{fade_in:other,fade_out:next});
  }
  function changeAtPointer(side:Side,clientX:number){
    const rect=trackRef.current?.getBoundingClientRect();
    if(!rect||rect.width<=0)return;
    const position=clamp((clientX-rect.left)/rect.width,0,1);
    change(side,(side==='in'?position:1-position)*valuesRef.current.length);
  }
  function beginDrag(event:PointerEvent<HTMLButtonElement>,side:Side){
    if(valuesRef.current.unavailable||event.button!==0||dragRef.current)return;
    event.preventDefault();event.stopPropagation();
    event.currentTarget.focus({preventScroll:true});
    const rect=event.currentTarget.getBoundingClientRect();
    dragRef.current={side,pointerId:event.pointerId,button:event.currentTarget,grabOffset:event.clientX-(rect.left+rect.width/2),startX:event.clientX,moved:false};
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(side);
  }
  function moveDrag(event:PointerEvent<HTMLButtonElement>){
    const drag=dragRef.current;
    if(!drag||drag.pointerId!==event.pointerId)return;
    event.preventDefault();event.stopPropagation();
    if(!drag.moved&&Math.abs(event.clientX-drag.startX)<2)return;
    drag.moved=true;
    changeAtPointer(drag.side,event.clientX-drag.grabOffset);
  }
  function endDrag(event:PointerEvent<HTMLButtonElement>,cancelled=false){
    const drag=dragRef.current;
    if(!drag||drag.pointerId!==event.pointerId)return;
    if(!cancelled&&drag.moved)changeAtPointer(drag.side,event.clientX-drag.grabOffset);
    releaseDrag();setDragging(null);
  }
  function keyboard(event:KeyboardEvent<HTMLButtonElement>,side:Side){
    if(event.nativeEvent.isComposing||event.nativeEvent.keyCode===229||event.ctrlKey||event.metaKey||event.altKey||!navigationKeys.has(event.key))return;
    event.preventDefault();event.stopPropagation();
    const latest=valuesRef.current;
    if(latest.unavailable)return;
    const current=side==='in'?latest.fadeIn:latest.fadeOut;
    const maximum=Math.max(0,latest.length-(side==='in'?latest.fadeOut:latest.fadeIn));
    const step=event.shiftKey?.1:.01;
    const next=event.key==='Home'?0:event.key==='End'?maximum:current+(
      event.key==='PageUp'?.1:event.key==='PageDown'?-.1:
      event.key==='ArrowRight'||event.key==='ArrowUp'?step:-step
    );
    change(side,next);
  }
  function preset(value:number){
    if(valuesRef.current.unavailable)return;
    const each=Math.min(value,Math.floor(valuesRef.current.length*500)/1000);
    valuesRef.current.onChange({fade_in:each,fade_out:each});
  }
  function handle(side:Side){
    const isIn=side==='in';
    const value=isIn?fadeIn:fadeOut;
    const maximum=Math.max(0,length-(isIn?fadeOut:fadeIn));
    return <button type="button" role="slider" className={`fade-handle ${isIn?'fade-in':'fade-out'}${dragging===side?' dragging':''}`}
      style={{left:`${(isIn?inRatio:outRatio)*100}%`}} disabled={unavailable}
      aria-label={`第 ${index+1} 段${isIn?'淡入':'淡出'}手柄`} aria-orientation="horizontal"
      aria-valuemin={0} aria-valuemax={maximum} aria-valuenow={value} aria-valuetext={seconds(value)}
      title={`${isIn?'向右拖动增加淡入':'向左拖动增加淡出'}；方向键微调`}
      onPointerDown={event=>beginDrag(event,side)} onPointerMove={moveDrag}
      onPointerUp={event=>endDrag(event)} onPointerCancel={event=>endDrag(event,true)}
      onLostPointerCapture={()=>{if(dragRef.current?.side===side){dragRef.current=null;setDragging(null);}}}
      onKeyDown={event=>keyboard(event,side)}>
      <svg viewBox="0 0 20 16" width="20" height="16" aria-hidden="true"><path d={isIn?'M3 12 13 3h4':'M3 3h4l10 9'}/><path className="fade-grip" d="M8 13h1m2 0h1"/></svg>
    </button>;
  }
  function curveSelector(side:Side){
    const isIn=side==='in';
    const selected=isIn?fadeInCurve:fadeOutCurve;
    const duration=isIn?fadeIn:fadeOut;
    const zeroHintId=`${curveGroupId}-${side}-zero`;
    return <div className={`fade-curve-group fade-curve-${side}`} role="radiogroup" aria-label={`第 ${index+1} 段${isIn?'淡入':'淡出'}曲线`} aria-describedby={duration===0?zeroHintId:undefined}>
      <div className="fade-curve-heading"><span>{isIn?'淡入':'淡出'}曲线</span><small>{FADE_CURVES.find(curve=>curve.value===selected)?.label}</small></div>
      <div className="fade-curve-options">
        {FADE_CURVES.map(curve=><label className="fade-curve-option" key={curve.value} title={curve.description}>
          <input type="radio" name={`${curveGroupId}-${side}`} value={curve.value} checked={selected===curve.value} disabled={unavailable} aria-label={curve.label} onChange={()=>{if(!valuesRef.current.unavailable)onCurveChange(side,curve.value);}}/>
          <span className="fade-curve-choice"><svg className="fade-curve-icon" viewBox="0 0 44 26" width="44" height="26" aria-hidden="true"><path className="fade-curve-grid" d="M3 3V23H41"/><path className="fade-curve-shape" d={fadeCurvePath(curve.value,side,3,3,38,20)}/></svg><span>{curve.label}</span></span>
        </label>)}
      </div>
      {duration===0&&<small className="fade-curve-zero" id={zeroHintId}>时长为 0，拖动{isIn?'上':'下'}方手柄后生效</small>}
    </div>;
  }

  return <section className={`fade-envelope${unavailable?' disabled':''}`} data-clip-index={index} aria-label={`第 ${index+1} 段淡入淡出`}>
    <header className="fade-envelope-heading"><strong>淡入淡出</strong><span>所选片段 · {seconds(length)}</span></header>
    <div className="fade-envelope-stage">
      <div className="fade-envelope-track" data-testid="fade-envelope-track" ref={trackRef}>
        <svg viewBox="0 0 1000 100" preserveAspectRatio="none" className="fade-envelope-visual" role="img" aria-label={`所选片段波形参考，淡入 ${seconds(fadeIn)} ${FADE_CURVES.find(curve=>curve.value===fadeInCurve)?.label}，淡出 ${seconds(fadeOut)} ${FADE_CURVES.find(curve=>curve.value===fadeOutCurve)?.label}`}>
          <path className="fade-midline" d="M0 50H1000"/>
          {bars.map((bar,barIndex)=><rect className="fade-source-bar" key={barIndex} x={bar.x} y={(100-bar.height)/2} width={Math.max(.5,bar.width*.72)} height={Math.max(.5,bar.height)}/>)}
          <path className="fade-region fade-region-in" d={`${inPath}L${inX} 0H0Z`}/>
          <path className="fade-region fade-region-out" d={`${outPath}L1000 0H${outX}Z`}/>
          <path className="fade-envelope-line" data-testid="fade-envelope-line" d={envelopePath}/>
          <path className="fade-boundary fade-boundary-in" d={`M${inX} 0V100`}/>
          <path className="fade-boundary fade-boundary-out" d={`M${outX} 0V100`}/>
        </svg>
        {!bars.length&&<span className="fade-no-wave">暂无波形参考 · 仍可拖动调整</span>}
        {handle('in')}{handle('out')}
      </div>
    </div>
    <p className="fade-envelope-hint">上方拖淡入 · 下方拖淡出 · 调整后按 K 试听</p>
    <div className="fade-value-fields">
      <label>淡入时长<div><input type="number" min="0" max={Math.max(0,length-fadeOut)} step="0.01" value={Number(finite(fadeIn).toFixed(3))} disabled={unavailable} aria-label={`第 ${index+1} 段淡入秒数`} onChange={event=>change('in',Number(event.target.value))}/><span>秒</span></div></label>
      <label>淡出时长<div><input type="number" min="0" max={Math.max(0,length-fadeIn)} step="0.01" value={Number(finite(fadeOut).toFixed(3))} disabled={unavailable} aria-label={`第 ${index+1} 段淡出秒数`} onChange={event=>change('out',Number(event.target.value))}/><span>秒</span></div></label>
    </div>
    <div className="fade-curve-selectors">{curveSelector('in')}{curveSelector('out')}</div>
    <div className="fade-presets" aria-label={`第 ${index+1} 段淡入淡出预设`}>
      <button type="button" disabled={unavailable} onClick={()=>preset(.05)} title="淡入、淡出各 0.05 秒；短片段自动缩短">轻柔起落 <span>0.05 s</span></button>
      <button type="button" disabled={unavailable} onClick={()=>preset(.3)} title="淡入、淡出各 0.3 秒；短片段自动缩短">缓慢起落 <span>0.3 s</span></button>
      <button type="button" className="fade-clear" disabled={unavailable||(!fadeIn&&!fadeOut)} onClick={()=>{if(!valuesRef.current.unavailable)valuesRef.current.onChange({fade_in:0,fade_out:0});}}>清除淡入淡出</button>
    </div>
  </section>;
}

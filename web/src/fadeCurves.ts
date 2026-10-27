import type {FadeCurve} from './api';

export const FADE_CURVES:ReadonlyArray<{value:FadeCurve;label:string;description:string}>=[
  {value:'linear',label:'直线',description:'音量匀速变化'},
  {value:'slow_fast',label:'先慢后快',description:'音量开始缓慢变化，随后加快'},
  {value:'fast_slow',label:'先快后慢',description:'音量开始快速变化，随后放缓'},
  {value:'s_curve',label:'S形',description:'音量两端缓慢变化，中段加快'},
];

/** t always advances with playback time; fade-out uses 1 - E(t). */
export function fadeProgress(curve:FadeCurve,t:number):number {
  const position=Math.max(0,Math.min(1,t));
  switch(curve){
    case 'slow_fast':return position*position;
    case 'fast_slow':return 1-(1-position)*(1-position);
    case 's_curve':return (1-Math.cos(Math.PI*position))/2;
    default:return position;
  }
}

/** Shared by the envelope, shaded regions and side-specific curve icons. */
export function fadeCurvePath(curve:FadeCurve,side:'in'|'out',x:number,y:number,width:number,height:number):string {
  const number=(value:number)=>String(Math.round(value*10000)/10000);
  return Array.from({length:33},(_,index)=>{
    const t=index/32;
    const progress=fadeProgress(curve,t);
    const gain=side==='in'?progress:1-progress;
    return `${index===0?'M':'L'}${number(x+t*width)} ${number(y+(1-gain)*height)}`;
  }).join('');
}

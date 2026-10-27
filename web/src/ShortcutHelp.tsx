import {Keyboard} from 'lucide-react';
import './shortcuts.css';

export default function ShortcutHelp({editor=false}:{editor?:boolean}) {
  const rows=[['空格 / K','播放 / 暂停'],['← / →','后退 / 前进 1 秒'],['Shift + ← / →','后退 / 前进 5 秒'],['Home','回到开头'],...(editor?[['Ctrl / ⌘ + S','保存当前试听'],['Esc','返回上一层 / 关闭窗口']]:[['M','静音 / 恢复音量'],['/','定位搜索']])];
  return <details className="shortcut-help"><summary aria-label="查看快捷键"><Keyboard size={15}/><span>快捷键</span></summary><div className="shortcut-card"><b>{editor?'剪辑器快捷键':'试听快捷键'}</b><dl>{rows.map(([key,label])=><div key={key}><dt><kbd>{key}</kbd></dt><dd>{label}</dd></div>)}</dl><p>{editor?'首次按空格会生成试听，参数修改后会重新生成。':'未开始试听时，空格播放当前选中的声音。'}输入文字时保留正常键盘操作；按钮可用 Enter 确认。</p></div></details>;
}

export interface Sound {
  id: string; name: string; tags: string[]; description: string; duration: number;
  source: 'builtin' | 'local' | 'freesound' | 'openverse' | 'elevenlabs' | 'edited' | 'kenney' | 'catalog' | 'localpack' | 'video'; source_id?: string;
  original_filename?: string; audio_track_count?: number;
  origin_source?: string; attribution?: string; download_label?: string;
  pack_id?: string; pack_name?: string; variant_group?: string; variant_count?: number;
  catalog_pack_bytes?: number; catalog_cached?: boolean; sample_rate?: number; channels?: number; bit_depth?: number; codec?: string;
  hidden?: boolean; audio_check?: AudioCheck; audio_check_error?: string;
  license: string; license_url: string; author: string; source_url: string;
  preview_url: string; waveform: number[]; favorite: boolean; folder_id: string | null;
  saved: boolean; downloaded: boolean; format: string; created_at: string; note: string; quality: string;
}
export interface Folder { id: string; name: string; parent_id: string | null; count: number }
export interface Settings {
  freesound_key_set: boolean; freesound_client_id: string; freesound_oauth_connected: boolean;
  elevenlabs_key_set: boolean; elevenlabs_license: 'unknown' | 'personal' | 'commercial';
  text_api_key_set: boolean; text_api_base: string; text_model: string; ai_translation: boolean; data_dir: string;
}
export interface Query { original: string; translated: string; terms: string[]; method: string; warning?: string }
export interface SearchResult { items: Sound[]; total: number; page?: number; has_more?: boolean; query?: Query; warning?: string; pagination_note?: string; collapsed_count?: number; hidden_count?: number }
export interface AudioCheck { version: number; status: 'ready' | 'unusable'; flags: string[]; peak_dbfs: number | null; rms_dbfs: number | null; estimated_peak_dbfs: number | null; gain_db: number | null; duration: number; leading_seconds: number; trailing_seconds: number; message: string }
export interface GamePack { id: string; name: string; description: string; count: number; bytes: number; installed_count: number }
export interface CatalogPack { id: string; name: string; description: string; author: string; license: string; license_url: string; count: number; bytes: number; source_url: string; origin_source: string }
export interface LocalPack { id: string; name: string; count: number; license: string }
export interface AudioTask { id: string; kind: 'pack' | 'check' | 'local-pack'; status: 'queued' | 'running' | 'complete' | 'cancelled' | 'failed'; completed: number; total: number; message: string; items: Sound[]; errors: string[] }
export interface CompensatedAudio { preview_url: string; gain_db: number; duration: number; waveform: number[]; message: string }
export type AudioFilter = 'all' | 'checked' | 'quiet' | 'issues' | 'unchecked';
export type VisibilityFilter = 'visible' | 'all' | 'hidden';
export interface Stats { total: number; favorites: number; generated: number; folders: number }
export interface Job { id: string; status: 'queued' | 'running' | 'complete' | 'failed'; error?: string; sound?: Sound }
export type FadeCurve = 'linear' | 'slow_fast' | 'fast_slow' | 's_curve';
export interface AudioClipRecipe { sound_id: string; start: number; end: number; gain_db: number; fade_in: number; fade_out: number; fade_in_curve?: FadeCurve; fade_out_curve?: FadeCurve }
export interface AudioEditRecipe { clips: AudioClipRecipe[]; master_gain_db: number; gap: number; format: 'wav' | 'mp3' }
export interface AudioEditPreview { id: string; preview_url: string; duration: number; waveform: number[]; format: string; warnings: string[] }
export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers);
  if (options?.method && options.method !== 'GET') {
    headers.set('X-Soundbox-Client', 'local');
    if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  }
  let response: Response;
  try { response = await fetch(`/api${path}`, {...options, headers}); }
  catch (error) { if (error instanceof DOMException && error.name === 'AbortError') throw error; throw new Error('连接不到本地服务。请重新双击「启动声匣」后再试。'); }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({detail: `请求未完成（${response.status}）`}));
    throw new Error(typeof detail.detail === 'string' ? detail.detail : '请求参数未被接受，请检查输入后重试。');
  }
  return response.json() as Promise<T>;
}
export const post = <T,>(path: string, body: unknown) => request<T>(path, {method:'POST', body:JSON.stringify(body)});
export const patch = <T,>(path: string, body: unknown) => request<T>(path, {method:'PATCH', body:JSON.stringify(body)});
export function errorText(error: unknown) { return error instanceof Error ? error.message : '操作未完成，请重试。'; }
export function fileDownload(id: string) { const link = document.createElement('a'); link.href = `/api/sounds/${encodeURIComponent(id)}/file`; link.download = ''; document.body.appendChild(link); link.click(); link.remove(); }
export function downloadEditorPreview(id: string, name: string) { const link = document.createElement('a'); link.href = `/api/editor/previews/${encodeURIComponent(id)}/download?${new URLSearchParams({name}).toString()}`; link.download = ''; document.body.appendChild(link); link.click(); link.remove(); }
export async function exportSounds(ids: string[]) {
  const response = await fetch('/api/export', {method:'POST',headers:{'Content-Type':'application/json','X-Soundbox-Client':'local'},body:JSON.stringify({ids})});
  if (!response.ok) {const data = await response.json().catch(()=>({detail:'导出失败，请检查音频是否已下载。'})); throw new Error(data.detail);}
  const blob = await response.blob(); const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = `声匣音效-${new Date().toISOString().slice(0,10)}.zip`; link.click(); setTimeout(()=>URL.revokeObjectURL(url), 15000);
}

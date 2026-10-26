/**
 * 对后端以 LF 分隔的 SSE 事件进行增量分帧。
 *
 * 传输块不必与事件边界对齐。解析器保留未完成的帧并拼接 data 字段；
 * 不负责解析 JSON、重连或持久化游标。换行状态跨块保留，包括拆开的 CR/LF 对。
 */

/**
 * 已解码的 SSE 字段；id 可以缺失，多行 data 使用换行符拼接。
 */
export interface SseFrame {
  id?: string
  event: string
  data: string
}

/**
 * 跨调用保留未完成帧的有状态解析器。
 *
 * 每条响应流创建一个解析器。到达 EOF 时不输出未结束的帧，
 * 也不负责重连或 Last-Event-ID 策略。
 */
export class SseParser {
  /** 最近一次完整空行边界之后的已解码文本。 */
  private buffer = ''
  /** CR 已产生换行时，即使 LF 位于下一块也需忽略。 */
  private previousWasCr = false

  /**
   * 追加已解码的传输块并返回当前所有完整帧。
   *
   * @param chunk - TextDecoder 输出而非原始字节，可包含部分帧或多个帧。
   * @returns 按到达顺序排列的帧；尾部未完成文本保留在缓冲区。
   * @remarks 跨块的 CR、LF 和 CRLF 分隔符统一为一个换行符。
   */
  push(chunk: string): SseFrame[] {
    for (const character of chunk) {
      if (character !== '\n' || !this.previousWasCr) {
        this.buffer += character === '\r' ? '\n' : character
      }
      this.previousWasCr = character === '\r'
    }
    const frames: SseFrame[] = []
    let boundary = this.buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const raw = this.buffer.slice(0, boundary)
      this.buffer = this.buffer.slice(boundary + 2)
      const frame = parseFrame(raw)
      if (frame !== null) frames.push(frame)
      boundary = this.buffer.indexOf('\n\n')
    }
    return frames
  }
}

/**
 * 解码一帧完整 SSE，忽略注释及不支持的字段。
 *
 * @param raw - 不含结尾空行的帧文本。
 * @returns data 字段由换行符拼接的帧；没有 data 字段时返回 null。
 */
function parseFrame(raw: string): SseFrame | null {
  let id: string | undefined
  let event = 'message'
  const data: string[] = []
  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue
    const separator = line.indexOf(':')
    const field = separator < 0 ? line : line.slice(0, separator)
    let value = separator < 0 ? '' : line.slice(separator + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'id') id = value
    else if (field === 'event') event = value
    else if (field === 'data') data.push(value)
  }
  if (data.length === 0) return null
  return { ...(id === undefined ? {} : { id }), event, data: data.join('\n') }
}

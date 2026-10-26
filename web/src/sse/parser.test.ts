import { describe, expect, it } from 'vitest'

import { SseParser } from './parser'

describe('SseParser', () => {
  it('preserves one frame at every CRLF transport split point', () => {
    const raw = 'id: cursor\r\ndata: first\r\ndata: second\r\n\r\n'
    for (let split = 0; split <= raw.length; split += 1) {
      const parser = new SseParser()
      expect([
        ...parser.push(raw.slice(0, split)),
        ...parser.push(raw.slice(split)),
      ]).toEqual([{ id: 'cursor', event: 'message', data: 'first\nsecond' }])
    }
  })

  it('accepts standalone CR delimiters with empty chunks between characters', () => {
    const parser = new SseParser()
    const frames = [...'data: hello\r\r'].flatMap((character) => [
      ...parser.push(character),
      ...parser.push(''),
    ])
    expect(frames).toEqual([{ event: 'message', data: 'hello' }])
  })
  it('parses split frames, ignores heartbeats, and joins data lines', () => {
    const parser = new SseParser()

    expect(parser.push(': ping\n\nid: epoch:1-0\nevent: message.block.')).toEqual([])
    expect(parser.push('delta\ndata: first\ndata: second\n\n')).toEqual([
      {
        id: 'epoch:1-0',
        event: 'message.block.delta',
        data: 'first\nsecond',
      },
    ])
  })
})

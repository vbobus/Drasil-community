/** Decode SSE over authenticated fetch (cookies or a local bearer session). */
export async function readEventStream(
  response: Response,
  onEvent: (name: string, payload: unknown) => void,
): Promise<void> {
  if (!response.body) throw new Error('This browser cannot receive a streaming response.');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let name = 'message';
  let data: string[] = [];
  const consume = (line: string) => {
    if (!line) {
      if (data.length) onEvent(name, JSON.parse(data.join('\n')) as unknown);
      name = 'message';
      data = [];
    } else if (line.startsWith('event:')) name = line.slice(6).trim();
    else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let end: number;
      while ((end = buffer.indexOf('\n')) !== -1) {
        consume(buffer.slice(0, end).replace(/\r$/, ''));
        buffer = buffer.slice(end + 1);
      }
      if (buffer.length > 2_000_000) throw new Error('The AI stream exceeded its message limit.');
      if (done) break;
    }
    if (buffer) consume(buffer.replace(/\r$/, ''));
    consume('');
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

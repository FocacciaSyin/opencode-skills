#!/usr/bin/env node
// 把 OpenCode session 轉成可讀 digest，供 recall / reflect / session-pickup 使用。
//
// 讀 /api/session/{id}/context，輸出使用者原話、assistant 文字與工具呼叫摘要。
// 只靠 `opencode` CLI（OpenCode 本身是 npm 套件，node 一定存在），不需要 PowerShell。
//
// 用法：
//   node session-digest.mjs --days 7
//   node session-digest.mjs --session ses_abc,ses_def
//   node session-digest.mjs --days 30 --user-only
//   node session-digest.mjs --days 7 --directory /path/to/project --out-dir /tmp/digest

import { spawnSync } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const USAGE = `用法：node session-digest.mjs [--days N | --session id1,id2] [options]

  --days N             近 N 天的 session（預設今天往前推）
  --session a,b        指定 session id（與 --days 擇一）
  --directory <path>   專案目錄，預設當前工作目錄
  --out-dir <path>     輸出目錄，預設 <tmpdir>/opencode/session-digest
  --max-assistant N    assistant 文字上限，預設 1200（超過保留頭 800 + 尾 400）
  --user-only          只輸出使用者訊息
  --include-subagents  納入 subagent session（預設排除）`

function parseArgs(argv) {
  const opts = {
    days: 0,
    session: [],
    directory: process.cwd(),
    outDir: join(tmpdir(), 'opencode', 'session-digest'),
    maxAssistant: 1200,
    userOnly: false,
    includeSubagents: false,
  }
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]
    switch (arg) {
      case '--days': opts.days = Number(argv[++i]); break
      case '--session': opts.session = String(argv[++i]).split(',').map(s => s.trim()).filter(Boolean); break
      case '--directory': opts.directory = argv[++i]; break
      case '--out-dir': opts.outDir = argv[++i]; break
      case '--max-assistant': opts.maxAssistant = Number(argv[++i]); break
      case '--user-only': opts.userOnly = true; break
      case '--include-subagents': opts.includeSubagents = true; break
      case '-h': case '--help': console.log(USAGE); process.exit(0); break
      default: console.error(`未知參數：${arg}\n\n${USAGE}`); process.exit(1)
    }
  }
  if (!opts.days && opts.session.length === 0) {
    console.error(`需要 --days 或 --session\n\n${USAGE}`)
    process.exit(1)
  }
  return opts
}

// Windows 上 opencode 是 .cmd，需要 shell；參數自行加引號避免 & 被解讀。
function apiGet(path) {
  const args = ['api', 'get', path]
  const result = process.platform === 'win32'
    ? spawnSync('opencode', args.map(a => `"${a}"`), { shell: true, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 })
    : spawnSync('opencode', args, { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 })
  if (result.error) throw result.error
  if (result.status !== 0) throw new Error(`opencode api 失敗（exit ${result.status}）：${result.stderr || result.stdout}`)
  return JSON.parse(result.stdout)
}

function redact(text) {
  if (!text) return text
  return text
    .replace(/Password=([^;"]+)/gi, 'Password=***')
    .replace(/Bearer\s+[A-Za-z0-9._-]+/gi, 'Bearer ***')
    .replace(/-P\s+\\?"[^"]*\\?"/gi, '-P "***"')
    .replace(/(--?password\s+)\S+/gi, '$1***')
}

// 太長時保留頭尾：結論通常在最前面（做了什麼）與最後面（結果）。
function clip(text, max) {
  if (!text || text.length <= max) return text
  const tail = 400
  const head = Math.max(max - tail, 200)
  const omitted = text.length - head - tail
  return `${text.slice(0, head)}\n…（略 ${omitted} 字）…\n${text.slice(-tail)}`
}

function resolveSessions(opts) {
  if (opts.session.length > 0) {
    // 明確指定時仍補上標題與時間，讓 digest 有上下文。
    return opts.session.map(id => {
      try {
        const info = apiGet(`/api/session/${id}`).data ?? {}
        return { id, title: info.title ?? '', updated: info.time?.updated ?? 0 }
      } catch {
        return { id, title: '', updated: 0 }
      }
    })
  }
  const cutoff = Date.now() - opts.days * 24 * 60 * 60 * 1000
  const all = []
  let cursor = null
  do {
    let path = `/api/session?limit=300&directory=${encodeURIComponent(opts.directory)}`
    if (cursor) path += `&cursor=${encodeURIComponent(cursor)}`
    const page = apiGet(path)
    all.push(...(page.data ?? []))
    cursor = page.cursor?.next ?? null
  } while (cursor)

  return all.filter(s => {
    if (s.time?.updated && s.time.updated < cutoff) return false
    if (!opts.includeSubagents && s.parentID) return false
    return true
  })
}

function buildDigest(session, opts) {
  const lines = [`=== SESSION ${session.id} ===`]
  if (session.title) lines.push(`title: ${session.title}`)
  if (session.time?.updated) {
    lines.push(`updated: ${new Date(session.time.updated).toISOString().slice(0, 16).replace('T', ' ')}`)
  }

  const ctx = apiGet(`/api/session/${session.id}/context`)
  const messages = ctx.data ?? []
  let contentCount = 0

  for (const message of messages) {
    if (message.type === 'user') {
      const text = redact(message.text ?? '')
      if (text.trim()) contentCount++
      lines.push(`### USER\n${text}`)
      continue
    }
    if (message.type !== 'assistant' || opts.userOnly) continue

    for (const part of message.content ?? []) {
      if (part.type === 'text' && part.text?.trim()) {
        contentCount++
        lines.push(`### ASSISTANT\n${clip(redact(part.text), opts.maxAssistant)}`)
      }
      if (part.type === 'tool') {
        contentCount++
        let input = ''
        try {
          input = redact(JSON.stringify(part.state?.input ?? {}))
        } catch {
          input = ''
        }
        if (input.length > 120) input = input.slice(0, 120)
        lines.push(`### TOOL ${part.name} [${part.state?.status ?? '?'}] ${input}`)
      }
    }
  }

  // 空 context（通常是整段已被壓縮）要明說，否則讀的人會以為工具壞了。
  if (contentCount === 0) {
    lines.push('')
    lines.push('### (無內容：context 為空，可能已全部被壓縮；請改用原始 session 或接受資料缺失)')
  }

  return { lines, contentCount }
}

const opts = parseArgs(process.argv.slice(2))
mkdirSync(opts.outDir, { recursive: true })

const sessions = resolveSessions(opts)
if (sessions.length === 0) {
  console.error(`找不到符合條件的 session（days=${opts.days || '未指定'}, directory=${opts.directory}）`)
  process.exit(1)
}

for (const session of sessions) {
  let digest
  try {
    digest = buildDigest(session, opts)
  } catch (error) {
    console.error(`跳過 ${session.id}：${error.message}`)
    process.exitCode = 1
    continue
  }
  const { lines, contentCount } = digest
  const file = join(opts.outDir, `${session.id}.txt`)
  writeFileSync(file, lines.join('\n') + '\n', 'utf8')
  const note = contentCount === 0 ? '  (空)' : ''
  console.log(`${session.id}  lines=${lines.length}${note}`)
}

console.log(`\n輸出目錄：${opts.outDir}`)

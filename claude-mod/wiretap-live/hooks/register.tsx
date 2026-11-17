import type { EngineInterface, Register } from 'claude-code'

// Where Wiretap listens. Pressing the button starts the server first when it
// isn't running. Nothing here talks to the model: 0 tokens.
const BASE = 'http://127.0.0.1:7878'

// the mod lives in <wiretap>/claude-mod/wiretap-live
function wiretapDir($: EngineInterface) {
  return $.plugin.root.replace(/\/claude-mod\/[^/]+\/?$/, '')
}

async function isUp($: EngineInterface) {
  const { stdout } = await $.process.run(
    ['curl', '-s', '-m', '1', '-o', '/dev/null', '-w', '%{http_code}', `${BASE}/api/session`],
    { timeoutMs: 3000 },
  )

  return stdout.trim() === '200'
}

async function openLive($: EngineInterface) {
  if (!(await isUp($))) {
    $.ui.toast('Starting Wiretap…')
    await $.process.run(
      ['sh', '-c', 'mkdir -p .wiretap-cache && nohup python3 -m wiretap serve >> .wiretap-cache/server.log 2>&1 &'],
      { cwd: wiretapDir($), timeoutMs: 5000 },
    )
    // the first index takes a couple of seconds
    for (let i = 0; i < 20 && !(await isUp($)); i++) {
      await $.process.run(['sleep', '0.5'])
    }
    if (!(await isUp($))) {
      return `Wiretap did not start. See ${wiretapDir($)}/.wiretap-cache/server.log`
    }
  }
  const sid = await $.session.id()
  await $.process.run(['open', `${BASE}/#v=live&sid=${encodeURIComponent(sid)}`])

  return 'Opened Wiretap Live in your browser.'
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'wiretap',
      description: 'Open what Claude is doing, live, in Wiretap (browser)',
    })

    return next(e)
  })

  on('command.run', { command: 'wiretap' }, async $ => ({ text: await openLive($) }))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) {
      return next(e)
    }
    const { Box, Button, Text } = $.ui.resolve(e)

    return (
      <Box>
        <Button
          key="wiretap-open"
          label="◉ Wiretap live ↗"
          hotkey="f"
          onPress={async () => $.ui.toast(await openLive($))}
        />
        <Text dimColor> click it, or type /wiretap</Text>
      </Box>
    )
  })
}

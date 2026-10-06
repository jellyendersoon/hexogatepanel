export function relayoutCodeEditorInstance(editor: unknown) {
  if (!editor || typeof editor !== 'object') return
  const e = editor as { layout?: () => void; resize?: () => void }
  if (typeof e.layout === 'function') e.layout()
  if (typeof e.resize === 'function') e.resize()
}

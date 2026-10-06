import { useRef } from 'react'

/**
 * Returns a ref that always points at the latest `value`.
 * Use it inside effects/callbacks that must read the current value without re-running when it changes.
 */
export function useLatest<T>(value: T) {
  const ref = useRef(value)
  ref.current = value
  return ref
}

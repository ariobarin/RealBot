// @vitest-environment jsdom
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { WordCarousel } from './WordCarousel'

vi.mock('framer-motion', async (original) => ({
  ...await original<typeof import('framer-motion')>(),
  useReducedMotion: () => true,
}))
afterEach(() => { cleanup(); vi.useRealTimers() })

it('still cycles words with reduced motion', () => {
  vi.useFakeTimers()
  const { container } = render(<WordCarousel words={['apartments', 'houses']} />)
  const word = () => container.querySelector('[aria-hidden="false"]')?.textContent
  expect(word()).toBe('apartments')
  act(() => { vi.advanceTimersByTime(900) })
  expect(word()).toBe('houses')
})

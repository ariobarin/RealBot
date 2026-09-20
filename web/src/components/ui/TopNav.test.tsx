// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { TopNav } from './TopNav'

const auth = vi.hoisted(() => ({ isLoading: true, session: null as unknown }))
vi.mock('../../auth/useAuth', () => ({ useAuth: () => auth }))
afterEach(cleanup)

it('does not present a restored account as signed out while it loads', () => {
  const page = () => <MemoryRouter><TopNav /></MemoryRouter>
  const result = render(page())
  expect(screen.queryByRole('link', { name: 'Realtor sign in' })).toBeNull()
  auth.isLoading = false
  auth.session = { role: 'realtor', email: 'test@example.com' }
  result.rerender(page())
  expect(screen.getByRole('button', { name: 'Account menu' })).toBeTruthy()
  auth.session = null
  result.rerender(page())
  expect(screen.getByRole('link', { name: 'Realtor sign in' })).toBeTruthy()
})

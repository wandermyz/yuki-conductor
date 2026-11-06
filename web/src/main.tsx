import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import MarkdownPage from './MarkdownPage.tsx'
import { initServiceWorker } from './webPush.ts'

initServiceWorker()

const fileId = new URLSearchParams(window.location.search).get('file')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {fileId ? <MarkdownPage fileId={fileId} /> : <App />}
  </StrictMode>,
)

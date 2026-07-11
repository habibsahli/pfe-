const ACTIVE_SESSION_KEY = "active_session_id"
const ACTIVE_SERVICE_KEY = "active_service_type"

function inBrowser(): boolean {
  return typeof window !== "undefined"
}

export function getActiveSessionId(): string {
  if (!inBrowser()) return ""
  return window.localStorage.getItem(ACTIVE_SESSION_KEY) || ""
}

export function setActiveSessionId(sessionId: string): void {
  if (!inBrowser() || !sessionId) return
  window.localStorage.setItem(ACTIVE_SESSION_KEY, sessionId)
  window.dispatchEvent(new CustomEvent("session-updated", { detail: { sessionId } }))
}

export function getActiveServiceType(): string {
  if (!inBrowser()) return "FIBRE"
  return window.localStorage.getItem(ACTIVE_SERVICE_KEY) || "FIBRE"
}

export function setActiveServiceType(serviceType: string): void {
  if (!inBrowser() || !serviceType) return
  window.localStorage.setItem(ACTIVE_SERVICE_KEY, serviceType)
  window.dispatchEvent(new CustomEvent("service-updated", { detail: { serviceType } }))
}

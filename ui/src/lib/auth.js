import { WebStorageStateStore } from "oidc-client-ts"

// Cognito OIDC config for react-oidc-context. All values come from the compute
// stack's `ui_env` output at build time (Vite injects import.meta.env.VITE_*).
const region = import.meta.env.VITE_AWS_REGION
const clientId = import.meta.env.VITE_COGNITO_CLIENT_ID
const hostedUiDomain = import.meta.env.VITE_COGNITO_DOMAIN

// The chat side-panel calls the chat AgentCore runtime DIRECTLY with the user's
// ACCESS token, which the runtime's JWT authorizer accepts on the okf-chat/invoke
// scope. So the SPA must REQUEST that scope at login (the app client is granted it
// in durable cognito.tf). VITE_CHAT_SCOPE is the full scope string from the
// compute ui_env output; default matches the resource server identifier.
const chatScope = import.meta.env.VITE_CHAT_SCOPE || "okf-chat/invoke"

export const cognitoAuthConfig = {
  // The Cognito issuer serves /.well-known/openid-configuration for OIDC
  // autodiscovery. VITE_COGNITO_AUTHORITY is exactly the issuer URL.
  authority: import.meta.env.VITE_COGNITO_AUTHORITY,
  client_id: clientId,
  redirect_uri: window.location.origin + "/callback.html",
  response_type: "code", // authorization-code + PKCE (S256); no client secret
  // openid/email/profile for login + the chat scope so the access token can call
  // the chat runtime directly. (The MCP scope is granted to the app client too,
  // but the SPA doesn't call MCP directly, so it isn't requested here.)
  scope: `openid email profile ${chatScope}`,
  post_logout_redirect_uri: window.location.origin + "/",

  // Full Cognito sign-out lives on the hosted-UI domain, not the issuer host.
  metadataSeed: hostedUiDomain
    ? {
        end_session_endpoint:
          `https://${hostedUiDomain}/logout` +
          `?client_id=${clientId}` +
          `&logout_uri=${encodeURIComponent(window.location.origin + "/")}`,
      }
    : undefined,

  // MPA: survive full page navigations across the HTML entry points.
  userStore: new WebStorageStateStore({ store: window.localStorage }),

  // Strip ?code=&state= after the redirect callback (else silent renew breaks).
  onSigninCallback: () => {
    window.history.replaceState({}, document.title, window.location.pathname)
  },
}

// Preserve the in-app location (hash route) across the OAuth round-trip: the
// login redirect goes to Cognito and returns to /callback.html, losing the
// original hash. Save it before redirecting; the callback page restores it.
const RETURN_HASH_KEY = "okf.returnHash"

export function signInPreservingRoute(auth) {
  try {
    if (window.location.hash) {
      sessionStorage.setItem(RETURN_HASH_KEY, window.location.hash)
    }
  } catch {
    // sessionStorage unavailable (private mode etc.) — proceed without it.
  }
  auth.signinRedirect()
}

// The path (with hash) the callback page should land on after sign-in. Consumes
// the saved hash so it applies once.
export function consumeReturnPath() {
  let hash = ""
  try {
    hash = sessionStorage.getItem(RETURN_HASH_KEY) || ""
    if (hash) sessionStorage.removeItem(RETURN_HASH_KEY)
  } catch {
    hash = ""
  }
  return "/" + hash
}

export { region }

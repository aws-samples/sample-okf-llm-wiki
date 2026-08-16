import { StrictMode, useEffect } from "react"
import { createRoot } from "react-dom/client"
import { AuthProvider, useAuth } from "react-oidc-context"

import "./index.css"
import { ThemeProvider } from "@/components/theme-provider.jsx"
import { cognitoAuthConfig, consumeReturnPath } from "@/lib/auth.js"
import { Spinner } from "@/components/ui/spinner.jsx"

// The OIDC redirect landing page. react-oidc-context processes ?code=&state=
// automatically; once authenticated we bounce back to the app, restoring the
// hash route the user was on before login (saved by signInPreservingRoute).
function Callback() {
  const auth = useAuth()
  useEffect(() => {
    if (auth.isAuthenticated) {
      window.location.replace(consumeReturnPath())
    } else if (auth.error) {
      window.location.replace("/")
    }
  }, [auth.isAuthenticated, auth.error])

  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-3">
      <Spinner />
      <p className="text-muted-foreground text-sm">
        {auth.error ? `Sign-in error: ${auth.error.message}` : "Signing you in…"}
      </p>
    </div>
  )
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <ThemeProvider>
      <AuthProvider {...cognitoAuthConfig}>
        <Callback />
      </AuthProvider>
    </ThemeProvider>
  </StrictMode>
)

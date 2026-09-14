import { Suspense, type ReactNode } from "react";
import { OperatorShell } from "./components/operator-shell";
import "./globals.css";
import "./mobile-shell.css";

export const metadata = { title: "Trading Engine — Operator" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="id"><body><Suspense fallback={children}><OperatorShell>{children}</OperatorShell></Suspense></body></html>;
}

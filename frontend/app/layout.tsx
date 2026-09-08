import type { ReactNode } from "react";
import { OperatorShell } from "./components/operator-shell";

export const metadata = { title: "Trading Engine — Operator" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="id"><body><OperatorShell>{children}</OperatorShell></body></html>;
}

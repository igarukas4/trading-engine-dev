import {
  createBindMountSandboxProvider,
  type BindMountCreateOptions,
  type BindMountSandboxHandle,
  type BindMountSandboxProvider,
  type SandboxProvider,
} from "@ai-hero/sandcastle";
import { randomUUID } from "node:crypto";
import { mkdtemp, rmdir, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

type RuntimeBindMountProvider = BindMountSandboxProvider & {
  create(options: BindMountCreateOptions): Promise<BindMountSandboxHandle>;
};

export function withFileBackedStdin(
  provider: SandboxProvider,
): BindMountSandboxProvider {
  if (!("sandboxHomedir" in provider)) {
    throw new Error("File-backed stdin requires a bind-mount sandbox provider.");
  }
  const runtimeProvider = provider as RuntimeBindMountProvider;

  return createBindMountSandboxProvider({
    name: provider.name,
    env: provider.env,
    sandboxHomedir: provider.sandboxHomedir,
    create: async (options) => {
      const handle = await runtimeProvider.create(options);

      return {
        ...handle,
        exec: async (command, execOptions) => {
          if (execOptions?.stdin === undefined) {
            return handle.exec(command, execOptions);
          }

          const temporaryDirectory = await mkdtemp(
            join(tmpdir(), "sandcastle-stdin-"),
          );
          const hostPath = join(temporaryDirectory, "input");
          const sandboxPath = `/home/agent/.sandcastle-stdin-${randomUUID()}`;
          const { stdin, ...optionsWithoutStdin } = execOptions;
          let copiedIntoSandbox = false;

          try {
            await writeFile(hostPath, stdin, "utf8");
            await handle.copyFileIn(hostPath, sandboxPath);
            copiedIntoSandbox = true;
            return await handle.exec(
              `${command} < '${sandboxPath}'`,
              optionsWithoutStdin,
            );
          } finally {
            let cleanupError: unknown;
            if (copiedIntoSandbox) {
              try {
                const cleanup = await handle.exec(`rm -f -- '${sandboxPath}'`);
                if (cleanup.exitCode !== 0) {
                  cleanupError = new Error(
                    `Failed to remove temporary sandbox input: ${cleanup.stderr}`,
                  );
                }
              } catch (error) {
                cleanupError = error;
              }
            }
            try {
              await unlink(hostPath);
            } catch (error) {
              if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
                cleanupError ??= error;
              }
            }
            try {
              await rmdir(temporaryDirectory);
            } catch (error) {
              cleanupError ??= error;
            }
            if (cleanupError) {
              throw cleanupError;
            }
          }
        },
      } satisfies BindMountSandboxHandle;
    },
  });
}

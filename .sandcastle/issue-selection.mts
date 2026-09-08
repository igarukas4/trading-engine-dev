export type ReadyIssue = {
  number: number;
  title: string;
  body: string;
};

const blockedBySection = /^## Blocked by\s*$([\s\S]*)/im;
const issueReference = /\/issues\/(\d+)/g;

function blockerNumbers(body: string): number[] {
  const section = body.match(blockedBySection)?.[1] ?? "";
  return [...section.matchAll(issueReference)].map((match) => Number(match[1]));
}

export function selectNextUnblockedIssue(
  issues: ReadyIssue[],
): ReadyIssue | undefined {
  const openIssueNumbers = new Set(issues.map((issue) => issue.number));

  return issues
    .filter((issue) =>
      blockerNumbers(issue.body).every(
        (blocker) => !openIssueNumbers.has(blocker),
      ),
    )
    .sort((left, right) => left.number - right.number)[0];
}

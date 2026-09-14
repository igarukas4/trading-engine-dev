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

export function selectUnblockedIssues(
  issues: ReadyIssue[],
  openIssueNumbers = new Set(issues.map((issue) => issue.number)),
): ReadyIssue[] {
  return issues
    .filter((issue) =>
      blockerNumbers(issue.body).every(
        (blocker) => !openIssueNumbers.has(blocker),
      ),
    )
    .sort((left, right) => left.number - right.number);
}

export function selectDispatchableIssues(
  readyIssues: ReadyIssue[],
  openIssueNumbers: Set<number>,
  plannedIssueNumbers: Set<number>,
  maxConcurrentIssues: number,
): ReadyIssue[] {
  return selectUnblockedIssues(readyIssues, openIssueNumbers)
    .filter((issue) => plannedIssueNumbers.has(issue.number))
    .slice(0, maxConcurrentIssues);
}

export function selectNextUnblockedIssue(
  issues: ReadyIssue[],
): ReadyIssue | undefined {
  return selectUnblockedIssues(issues)[0];
}

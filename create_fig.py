"""Script to generate the README.md file of my profile.

- Grabs my public repos.
- Filters for those which arent forks.
- Computes the total lines of code.
- Aggregates the total of the different projects.
- Creates a figure with the info.
- Creates the readme from a template including the info obtained.

Example:
$ python create_readme.py <token>/{secrets.PLAGUSS_TOKEN_README} > README_TEST.md
"""

import argparse
import dataclasses
import datetime as dt
import pathlib
import shelve
import subprocess
import tempfile
from shelve import DbfilenameShelf

import gidgethub.abc
import gidgethub.httpx
import httpx
import iso8601
import matplotlib.pyplot as plt
import numpy as np
import trio
from pytokei import Config, Languages

USERNAME = "plaguss"
# This script is expected to run once a week,
# the timedelta should change otherwise.
here = pathlib.Path(__file__).parent.resolve()
dbname = str(here / "checkpoint_db")

Header = tuple[str, str, str, str, str, str]
ReportLine = tuple[str, int, int, int, int, int]


@dataclasses.dataclass
class LanguageReport:
    """Dataclass representing a report of a language from pytokei."""

    language: str
    files: int = 0
    lines: int = 0
    code: int = 0
    comments: int = 0
    blanks: int = 0

    def merge(self, report: "LanguageReport") -> None:
        """Only adds the content if they pertain to the same language."""
        if report.language == self.language:
            self.files += report.files
            self.lines += report.lines
            self.code += report.code
            self.comments += report.comments
            self.blanks += report.blanks

    def report_line(self) -> ReportLine:
        return (
            self.language,
            self.files,
            self.lines,
            self.code,
            self.comments,
            self.blanks,
        )

    def __hash__(self):
        return hash(self.report_line())


@dataclasses.dataclass
class RepoReport:
    """Report of a whole repo.
    A single report repo can be updated to have the info in a single point.

    Attributes:
        name : str
            Name of the repo.
        reports : str
            dict with the name of the language and the corresponding content.
    """

    name: str = ""
    reports: dict[str, LanguageReport] = dataclasses.field(default_factory=dict)

    def insert(self, language_report: LanguageReport) -> None:
        """Adds a LanguageReport, or updates one if exists."""
        if language_report.language in self.reports.keys():
            self.reports[language_report.language].merge(language_report)
        else:
            self.reports[language_report.language] = language_report

    def merge(self, repo_report: "RepoReport") -> None:
        """Adds a whole report, merging the info."""
        for _, language_report in repo_report.reports.items():
            self.insert(language_report)

    def as_table(self) -> tuple[Header, list[ReportLine]]:
        """Returns the report as a tuple with the header and a list of reports."""
        header: Header = ("Language", "Files", "Lines", "Code", "Comments", "Blanks")
        table = [report.report_line() for _, report in self.reports.items()]
        return (header, table)


def create_repo_report(name: str, locs: dict[str, dict[str, int]]) -> RepoReport:
    """Creates a RepoReport from the output of grab_loc and the name given.
    This info comes from repo_name in projects response.
    """
    langs_report = RepoReport(name)
    for lang_name, lang_report in locs.items():
        lreport = LanguageReport(lang_name, **lang_report)
        langs_report.insert(lreport)
    return langs_report


class RepoWalker:
    """Visits the repos and checks if something is already up to date.

    db is a shelve object which contains an entry for each repo, with the
    repo name as a key, and stores the `last_update` (with a dt.datetime
    from the last time this repo was visited) and a `repo_report`
    with the `RepoReport` object.
    """

    def __init__(self) -> None:
        # The current date is used to check for updates
        self.current_date: dt.datetime = dt.datetime.now().replace(tzinfo=iso8601.UTC)
        self.db: DbfilenameShelf = shelve.open(dbname, "c")
        self.repo_report: RepoReport = RepoReport()

    def insert(self, repo_report: RepoReport) -> None:
        self.repo_report.merge(repo_report)

    def contains(self, repo_name: str) -> bool:
        return repo_name in self.db.keys()

    def register(self, repo_report: RepoReport) -> None:
        """Updates the report in the db and sets the date of update."""
        self.db[repo_report.name] = {
            "repo_report": repo_report,
            "last_update": self.current_date,
        }

    def get(self, repo_name: str) -> RepoReport:
        """Gets a report from the database or returns a ValueError.
        It should never raise the error, it must be filled before calling
        this function.
        """
        if report := self.db.get(repo_name):
            return report["repo_report"]
        raise ValueError(f"Report not found: {repo_name}")

    def run(self, projects: list[tuple[str, dt.datetime, str]]):
        """Checks the repos
        for each project detected.
            if new project
                clone it
                grab loc
                create RepoReport
                register to RepoWalker
                RepoWalker must register in the db the content and last_update
            if old project
                if pushed_date prior to current date
                    get RepoReport from db
                    merge RepoReport inside RepoWalker's RepoReport
                else
                    clone it
                    grab loc
                    create RepoReport
                    register to RepoWalker
                    RepoWalker must register in the db the content and last_update
        close db in RepoWalker
        """
        for clone_url, pushed_date, repo_name in projects:

            if self.contains(repo_name):
                if pushed_date < self.db[repo_name]["last_update"]:
                    print(f"Repo registered without updates: {repo_name}")
                    repo_report: RepoReport = self.get(repo_name)

                else:
                    print(f"Old repo, updated contents: {repo_name}")
                    repo_report = visit_repo(repo_name, clone_url)
                    self.register(repo_report)

            else:
                print(f"New repo! lets visit: {repo_name}")
                repo_report = visit_repo(repo_name, clone_url)
                self.register(repo_report)

            self.insert(repo_report)

        self.db.close()


def visit_repo(repo_name: str, clone_url: str):
    print(f"Cloning repo {repo_name} and running pytokei.")
    with tempfile.TemporaryDirectory() as tmpdirname:
        clone_repo(clone_url, tmpdirname)
        locs = grab_loc(str(pathlib.Path(tmpdirname) / repo_name))
        repo_report = create_repo_report(repo_name, locs)
    return repo_report


async def get_projects(
    gh: gidgethub.abc.GitHubAPI, username: str
) -> list[tuple[str, dt.datetime, str]]:
    """Obtains from all the projects which arent fork, a tuple with
    the following fields:

    - clone_url: URL to clone via git.
    - pushed_at: The last time a change was done to the repo, to
        avoid cloning it again if it has no changes.
    - name: Name of the repo, which will be the name of the folder
        when cloned.

    https://docs.github.com/en/rest/repos/repos#list-repositories-for-a-user
    """
    projects = await gh.getitem(
        "/users/{username}/repos",
        {"username": username},
        accept="application/vnd.github.v3+json",
    )
    return _parse_projects(projects)


def _parse_projects(projects):
    """The function is split in two parts to simplify testing."""
    return [
        (p["clone_url"], iso8601.parse_date(p["pushed_at"]), p["name"])
        for p in projects
        if not p["fork"]
    ]


def grab_loc(project_path: str) -> dict[str, dict[str, int]]:
    """Uses pytokei to obtain the lines of code in a project."""
    langs: Languages = Languages()
    langs.get_statistics([project_path], ["*.json", "*.svg", "*.SVG"], Config())
    return langs.report_compact_plain()


def clone_repo(repo_url: str, cwd: str) -> None:
    """Clones a repo relative to cwd."""
    subprocess.run(["git", "clone", repo_url], check=True, cwd=cwd)


def generate_figure(repo_report: RepoReport, figtype: list[str] = ["lines"]) -> None:
    """Creates the svg figure to be inserted in the template.

    It uses a RepoReport obtained after calling RepoWalker.run()

    figtype argument is used to determine what variables should be
    present in the figure. There possibilities come from
    ["lines", "code", "comments", "blanks"].
    If more than one of them is set, the bars will be stacked in the figure.
    """
    headers, content = repo_report.as_table()
    content = np.array(content)
    languages = content[:, 0]
    # Get the numbers, but the number of files are removed for the moment,
    # include from 1: if wanted.
    values = content[:, 2:].astype(int)
    # The values are sorted according to the number of lines in ascending order
    sorted_idx = np.argsort(values[:, 0])
    # For some reason SVG isn't ignored, remove it here
    print("Languages found: ", languages)
    idx_svg = languages.index("SVG")
    sorted_languages = np.take_along_axis(languages, sorted_idx, axis=0)
    # we need the arrays to have the same dimension
    sorted_values = np.take_along_axis(values, sorted_idx[:, np.newaxis], axis=0)
    # use the cumulated sum to avoid overlapping on the bars
    sorted_values_ = sorted_values[:, 1:]
    sorted_values = sorted_values_.cumsum(axis=1)
    with plt.xkcd():
        fig, ax = plt.subplots(figsize=(12, 8))
        y_pos = np.arange(len(sorted_languages))
        # Insert the figures in inverted order
        # width = 0.15
        b1 = ax.barh(y_pos, sorted_values[:, 2], color="xkcd:easter green")
        b2 = ax.barh(y_pos, sorted_values[:, 1], color="xkcd:light lavendar")
        b3 = ax.barh(y_pos, sorted_values[:, 0], color="xkcd:soft blue")

        ax.bar_label(b1, label_type="edge")
        ax.bar_label(b2, label_type="edge")
        ax.bar_label(b3, label_type="edge")

        ax.set_xlabel("Number of lines")
        ax.set_yticks(y_pos, labels=sorted_languages)
        ax.legend(headers[-3:][::-1], loc="lower right", fontsize="small")  # bbox_to_anchor=(0, 1)
        ax.set_title(f"What languages should you expect\n in my public repos?\n last updated: {dt.date.today().isoformat()}")
        plt.tight_layout()
    fig.savefig("pytokei_fig.svg")


async def main(token: str, username: str = USERNAME) -> None:
    """Note for my future self.
    This doesn't need async, initially I thought there would
    be more calls to github's API and it would be interesting,
    then it just wasn't bad enough to change it.
    """
    repo_walker = RepoWalker()
    async with httpx.AsyncClient() as client:
        gh = gidgethub.httpx.GitHubAPI(client, "plaguss", oauth_token=token)
        projects = await get_projects(gh, username)
        repo_walker.run(projects)
        # After the RepoWalker has finished, get the RepoReport with the content

    generate_figure(repo_walker.repo_report)
    print("Done!")


if __name__ == "__main__":
    # Set the token as a secret in github to allow it running from a GA workflow
    parser = argparse.ArgumentParser(prog="Readme generator")
    parser.add_argument("token")
    args = vars(parser.parse_args())
    # To run locally, just grab the token from the env
    # from dotenv import dotenv_values
    # conf = dotenv_values(".env")
    # token = conf.get("PLAGUSS_TOKEN_README")
    token = args["token"]
    trio.run(main, token, USERNAME)

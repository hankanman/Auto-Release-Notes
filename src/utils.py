""" This module contains utility functions for the script. """

import sys
import re
import datetime
import logging as log
import json
from pathlib import Path
from dataclasses import dataclass
from typing import List
import asyncio
import aiohttp
from .enums import (
    LogLevel,
    ResponseStatus,
    APIEndpoint,
)
from .config import Config, Prompts, DevOpsConfig, ModelConfig


@dataclass
class AuthConfig:
    """Represents authentication configuration settings."""

    pat: str
    gpt_api_key: str


@dataclass
class OutputConfig:
    """Represents output configuration settings."""

    output_folder: Path
    software_summary: str


@dataclass
class ScriptConfig:
    """Represents the overall configuration settings for the script."""

    auth: AuthConfig
    devops: DevOpsConfig
    model: ModelConfig
    parent_work_item_types: List[str]
    output: OutputConfig


def setup_logs(level: LogLevel = LogLevel.INFO):
    """
    Sets up logging configuration with the specified logging level.

    Parameters:
        level (LogLevel): The logging level to set. Defaults to LogLevel.INFO.

    Returns:
        None
    """
    log.basicConfig(
        level=level.value, format="%(asctime)s - %(levelname)s - %(message)s"
    )


def create_contents(input_array: List[str]) -> str:
    """
    Converts a Array of section headers into a markdown table of contents.

    Args:
        input_array (Array[str]): The Array of section headers.

    Returns:
        str: The markdown table of contents.

    """
    markdown_links = []
    for item in input_array:
        anchor = re.sub(r"[^\w-]", "", item.replace(" ", "-")).lower()
        markdown_links.append(f"- [{item}](#{anchor})\n")
    return "".join(markdown_links)


def format_date(date_str: str) -> str:
    """Format the modified date string.

    Args:
        date_str (str): Input date string in the format "%Y-%m-%dT%H:%M:%S.%fZ"

    Returns:
        str: Human-readable date string in the format "%d-%m-%Y %H:%M"
    """
    try:
        date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        return date_obj.strftime("%d-%m-%Y %H:%M")
    except ValueError:
        log.warning("Invalid modified date format: %s", date_str)
        return date_str


def clean_string(string: str, min_length: int = 30) -> str:
    """Strip a string of HTML tags, URLs, JSON, and user references."""
    string = re.sub(r"<[^>]*?>", "", string)  # Remove HTML tags
    string = re.sub(r"http[s]?://\S+", "", string)  # Remove URLs
    string = re.sub(r"@\w+(\.\w+)?", "", string)  # Remove user references

    try:
        json.loads(string)
        string = ""
    except json.JSONDecodeError:
        pass

    string = string.strip()
    string = re.sub(r"&nbsp;", " ", string)
    string = re.sub(r"\s+", " ", string)
    return string if len(string) >= min_length else ""


def count_tokens(text: str) -> int:
    """
    Calculates the token count for a given text.

    Parameters:
    text (str): The input text for which the token count needs to be calculateDevOpsConfig.

    Returns:
    int: The total count of tokens in the given text.
    """
    word_count = len(re.findall(r"\b\w+\b", text))
    char_count = len(re.sub(r"\s", "", text))
    return word_count + char_count


async def summarise(prompt: str, session: aiohttp.ClientSession) -> str:
    """
    Sends a prompt to GPT and returns the response.

    Args:
        prompt (str): The prompt to be sent to GPT.

    Returns:
        str: The response generated by GPT.
    """
    model_objects = {model["Name"]: model for model in ModelConfig.models}
    model_object = model_objects.get(ModelConfig.model)
    token_count = count_tokens(prompt)
    if model_object and token_count > model_object["Tokens"]:
        log.warning(
            "The prompt contains too many tokens for the selected model %s/%s. Please reduce the size of the prompt.",
            token_count,
            model_object["Tokens"],
        )
        return ""

    retry_count = 0
    initial_delay = 10
    max_retries = 6
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ModelConfig.gpt_api_key}",
    }
    payload = {
        "model": ModelConfig.model,
        "messages": [{"role": "user", "content": prompt}],
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        while retry_count <= max_retries:
            try:
                async with session.post(
                    ModelConfig.gpt_base_url + APIEndpoint.COMPLETIONS.value,
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    result = await response.json()
                    if response.status != 200:
                        log.error(result["message"])
                        sys.exit(1)
                    return str(result["choices"][0]["message"]["content"])
            except aiohttp.ClientResponseError as e:
                if e.status == ResponseStatus.RATE_LIMIT.value:
                    delay = initial_delay * (2**retry_count)
                    log.warning(
                        "AI API Error (Too Many Requests), retrying in %s seconds...",
                        delay,
                    )
                    await asyncio.sleep(delay)
                    retry_count += 1
                elif e.status == ResponseStatus.ERROR.value:
                    delay = initial_delay * (2**retry_count)
                    log.warning(
                        "AI API Error (Internal Server Error), retrying in %s seconds...",
                        delay,
                    )
                    await asyncio.sleep(delay)
                    retry_count += 1
                elif e.status == ResponseStatus.NOT_FOUND.value:
                    log.error(
                        "AI API Key Error, this is usually because you are using a free account rather than a paid one.",
                        exc_info=True,
                    )
                    return ""
                else:
                    log.error("Request failed", exc_info=True)
                    return ""
        log.error("Max retries reached. Request failed.")
        return ""


async def finalise_notes(html: bool, summary_notes: str, file_md: Path) -> None:
    """
    Finalizes the release notes by adding the summary and table of contents.

    Args:
        html (bool): A boolean flag indicating whether to generate HTML output.
        summary_notes (str): The summary of the work items completed in this release.
        file_md (Path): The path to the output Markdown file.
        file_html (Path): The path to the output HTML file.
        section_headers (Array[str]): A Array of section headers for the table of contents.

    Returns:
        None
    """
    log.info("Writing final summary and table of contents...")
    final_summary = await summarise(
        f"{Prompts.summary}{Config.software_summary}\n"
        f"The following is a summary of the work items completed in this release:\n"
        f"{summary_notes}\nYour response should be as concise as possible",
        aiohttp.ClientSession(),
    )
    with open(file_md, "r", encoding="utf-8") as file:
        file_contents = file.read()

    file_contents = file_contents.replace("<NOTESSUMMARY>", str(final_summary))
    # TODO: Add table of contents
    # toc = create_contents(section_headers)
    # file_contents = file_contents.replace("<TABLEOFCONTENTS>", toc)
    file_contents = file_contents.replace(" - .", " - AddresseDevOpsConfig.")

    if html:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.github.com/markdown",
                json={"text": file_contents},
                headers={"Content-Type": "application/json"},
            ) as markdown_response:
                markdown_text = await markdown_response.text()
                file_html = file_md.with_suffix(".html")
                with open(file_html, "w", encoding="utf-8") as file:
                    file.write(markdown_text)

    with open(file_md, "w", encoding="utf-8") as file:
        file.write(file_contents)

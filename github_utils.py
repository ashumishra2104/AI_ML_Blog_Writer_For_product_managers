import os
import re
import base64
from pathlib import Path
from typing import List, Optional
from github import Github, GithubException, InputGitTreeElement

def push_to_github(
    repo_name: str,
    branch: str,
    token: str,
    md_content: str,
    md_filename: str,
    images_dir: str = "images",
    commit_message: str = "Add blog post",
) -> str:
    """
    Pushes a markdown file and its referenced images to a GitHub repository.
    Handles empty repositories by performing an initial commit if needed.
    """
    g = Github(token)
    try:
        repo = g.get_repo(repo_name)
    except GithubException as e:
        raise Exception(f"Could not access repository '{repo_name}': {e.data.get('message', str(e))}")

    # 1. Check if repository is empty and initialize if needed
    # -------------------------------------------------------
    is_empty = False
    try:
        # If this fails, the repo is likely empty
        repo.get_contents("/")
    except GithubException as e:
        if e.status == 404: # Repo is empty
            is_empty = True
        else:
            raise e

    if is_empty:
        # Initialize with a README.md to create the branch
        try:
            repo.create_file(
                "README.md",
                "Initial commit",
                "# PM Blog Repository\n\nAutomatically generated repository for PM-tuned AI blogs.",
                branch=branch
            )
        except GithubException as e:
            # If creating README fails, it might be a different issue
            raise Exception(f"Failed to initialize repository: {e.data.get('message', str(e))}")

    # 2. Collect all files to push
    # -----------------------------
    elements = []
    
    # Add markdown file
    elements.append(InputGitTreeElement(
        path=md_filename,
        mode='100644',
        type='blob',
        content=md_content
    ))

    # Parse markdown for image references
    img_matches = re.findall(r"!\[.*?\]\((images/.*?)\)", md_content)
    for img_path_str in img_matches:
        img_path = Path(img_path_str)
        if img_path.exists():
            with open(img_path, "rb") as f:
                content = f.read()
            # Image blobs work fine after the repo is initialized
            blob = repo.create_git_blob(base64.b64encode(content).decode("utf-8"), "base64")
            elements.append(InputGitTreeElement(
                path=img_path_str,
                mode='100644',
                type='blob',
                sha=blob.sha
            ))

    # 3. Get current branch reference
    # -------------------------------
    try:
        ref = repo.get_git_ref(f"heads/{branch}")
        base_tree = repo.get_git_tree(ref.object.sha)
    except GithubException:
        # If branch doesn't exist (unexpected since we just initialized, but possible if branch name differs)
        try:
            main_ref = repo.get_git_ref("heads/main")
        except GithubException:
            main_ref = repo.get_git_ref("heads/master")
        
        repo.create_git_ref(ref=f"refs/heads/{branch}", sha=main_ref.object.sha)
        ref = repo.get_git_ref(f"heads/{branch}")
        base_tree = repo.get_git_tree(ref.object.sha)

    # 4. Create and update with a single commit
    new_tree = repo.create_git_tree(elements, base_tree)
    parent = repo.get_git_commit(ref.object.sha)
    commit = repo.create_git_commit(commit_message, new_tree, [parent])
    
    # Update the reference
    ref.edit(commit.sha)

    return f"https://github.com/{repo_name}/blob/{branch}/{md_filename}"

import os
import click
import shlex
import subprocess
from ..config import config as app_config
from ..utils.git import get_git_diff, smart_git_add, ensure_git_root, get_current_branch, get_remote_branches
from ..api.client import api_client


def run_git_command_with_ai(command, dry_run=False):
    """Git command wrapper that includes AI error assistance."""
    from ..utils.git import run_git_command
    
    def ai_helper(cmd, error_msg):
        return api_client.git_error_analysis(cmd, error_msg)
    
    return run_git_command(command, dry_run=dry_run, ai_helper_func=ai_helper)


def get_push_command(target_branch, new_branch=None):
    """Get the appropriate push command based on local configuration."""
    # Get local repository configuration
    local_repo_config = app_config.get('_local_repository', {})
    
    if not local_repo_config:
        # No local config, use default GitHub-style push
        remote = 'origin'
        return ['git', 'push', '--set-upstream', remote, target_branch]
    
    # Get configuration values
    remote = local_repo_config.get('remote', 'origin')
    push_template = local_repo_config.get('push_command', 'git push {remote} {branch}')
    target_branch_config = local_repo_config.get('target_branch', 'main')
    repo_type = local_repo_config.get('type', 'github')
    
    # Variables for template substitution
    variables = {
        'remote': remote,
        'branch': target_branch,
        'target_branch': target_branch_config
    }
    
    # Format the push command
    try:
        formatted_command = push_template.format(**variables)
        # Convert to command list
        command_parts = shlex.split(formatted_command)
        return command_parts
    except Exception as e:
        click.echo(f"Warning: Invalid push command template: {e}")
        click.echo(f"Falling back to default push command")
        return ['git', 'push', '--set-upstream', remote, target_branch]


def show_push_preview(target_branch, new_branch=None):
    """Show what push command will be executed."""
    local_repo_config = app_config.get('_local_repository', {})
    
    if local_repo_config:
        repo_type = local_repo_config.get('type', 'github')
        push_command = get_push_command(target_branch, new_branch)
        push_cmd_str = ' '.join(push_command)
        
        click.echo(f"📦 仓库类型: {repo_type}")
        click.echo(f"🚀 推送命令: {push_cmd_str}")
        
        # Show explanation for special repository types
        if repo_type == 'gerrit':
            click.echo("💡 Gerrit 推送说明: 代码将推送到 refs/for/ 等待代码评审")
        elif repo_type == 'gitlab':
            click.echo("💡 GitLab 推送说明: 代码将推送到 GitLab 仓库")
        elif repo_type == 'gitee':
            click.echo("💡 Gitee 推送说明: 代码将推送到 Gitee 仓库")
    else:
        click.echo("📦 使用默认 GitHub 风格推送")
        click.echo(f"🚀 推送命令: git push --set-upstream origin {target_branch}")


def handle_push_command(new_branch, dry_run, style, check_bugs, no_check_bugs):
    """Implementation of the push command."""
    
    # 确保在Git根目录执行
    git_root, original_cwd = ensure_git_root()
    if git_root is None:
        return
    
    try:
        if dry_run:
            click.secho("[DRY RUN MODE] Preview of operations:", fg="blue", bold=True)
        
        # 1. Create and switch to a new branch if specified
        if new_branch:
            click.echo(f"› Creating new branch '{new_branch}'...")
            if not run_git_command_with_ai(['git', 'checkout', '-b', new_branch], dry_run):
                return # Error is printed by the helper

        # 2. Add all changes
        click.echo("› Staging all changes...")
        if not smart_git_add(dry_run):
            click.echo("Error: Failed to stage changes.")
            return
        
        # Check if there are any changes to commit after adding
        if not dry_run:
            diff = get_git_diff(staged=True)
            if not diff:
                click.echo("No changes to commit. Working directory is clean.")
                return
        else:
            # In dry run, simulate having changes
            click.echo("[DRY RUN] Simulating changes for demonstration...")
            diff = "simulated diff for dry run"

        # Determine if bug check should be run
        should_check_bugs = check_bugs or (
            app_config.get('commit', {}).get('check_bugs_by_default', False) and not no_check_bugs
        )

        # Run bug check if requested or configured by default
        if should_check_bugs and not dry_run:
            click.echo("› Running comprehensive code quality analysis...")
            quality_result = api_client.code_quality_check(diff)
            
            click.secho("\n🔍 Code Quality Analysis Results:", fg="blue", bold=True)
            click.echo("-" * 40)
            analysis = quality_result.get("summary", "No analysis available")
            click.echo(analysis)
            overall_score = quality_result.get("overall_score", 0.0)
            click.echo(f"\nOverall Quality Score: {overall_score}/10.0")
            click.echo("-" * 40)
            
            # Ask user if they want to continue based on score
            if overall_score < 6.0:
                if not click.confirm("\nCode quality issues detected. Do you want to continue with the push?", default=False):
                    click.echo("Push cancelled due to code quality issues.")
                    return
            else:
                click.echo("✅ Code quality looks good!")
        elif should_check_bugs and dry_run:
            click.echo("› [DRY RUN] Would run bug analysis here...")
            
        # 3. Generate commit message
        used_style = style or app_config.get('commit', {}).get('style', 'conventional')
        click.echo(f"› Generating commit message with AI ({used_style} style)...")
        
        if not dry_run:
            # 使用服务端API生成提交消息
            commit_message = api_client.generate_commit_message(diff, used_style)
        else:
            # Generate example message based on style for dry run
            style_examples = {
                'conventional': "[DRY RUN] feat: example conventional commit",
                'semantic': "[DRY RUN] Add example feature implementation",
                'simple': "[DRY RUN] Example feature added",
                'emoji': "[DRY RUN] ✨ Add example feature"
            }
            commit_message = style_examples.get(used_style, "[DRY RUN] feat: example commit message")

        if not commit_message or commit_message.startswith("Failed to connect") or commit_message.startswith("Error"):
            if not dry_run:
                click.echo(f"✗ Failed to generate commit message: {commit_message}")
                click.echo("Please check your server connection and configuration.")
                return
            # For dry run, continue with example message

        click.echo(f"✓ Generated commit message:\n  {commit_message}")
        
        # 4. Commit
        if not run_git_command_with_ai(['git', 'commit', '-m', commit_message], dry_run):
            return
        if not dry_run:
            click.echo("✓ Successfully committed.")

        # 5. Determine target branch
        target_branch = new_branch
        if not target_branch:
            if not dry_run:
                branch_result = run_git_command_with_ai(['git', 'rev-parse', '--abbrev-ref', 'HEAD'])
                if not branch_result:
                    return
                target_branch = branch_result.stdout.strip()
            else:
                target_branch = "current-branch"

        # 6. Show push preview and execute push
        click.echo(f"› Pushing to branch '{target_branch}'...")
        
        # Show push command preview
        show_push_preview(target_branch, new_branch)
        
        # Get the appropriate push command
        push_command = get_push_command(target_branch, new_branch)
        
        if not run_git_command_with_ai(push_command, dry_run):
            return
            
        if not dry_run:
            click.echo(f"✓ Successfully pushed!")
            
            # Show additional info for special repository types
            local_repo_config = app_config.get('_local_repository', {})
            if local_repo_config.get('type') == 'gerrit':
                click.echo("💡 代码已推送到 Gerrit，请查看代码评审页面")
            elif local_repo_config.get('type') == 'gitlab':
                click.echo("💡 代码已推送到 GitLab")
        else:
            click.secho("\n[DRY RUN COMPLETE] No actual changes were made.", fg="blue", bold=True)
    
    finally:
        # 切换回原始目录
        if original_cwd and original_cwd != git_root:
            os.chdir(original_cwd) 


@click.command()
@click.option('--branch', default=None, help='目标分支 (默认为origin/main)')
@click.option('--message', '-m', default=None, help='提交消息')
@click.option('--auto-commit', is_flag=True, help='自动生成提交消息并提交')
@click.option('--dry-run', is_flag=True, help='仅显示将要执行的操作，不实际执行')
def push(branch, message, auto_commit, dry_run):
    """智能推送代码"""
    
    try:
        # 获取当前分支
        current_branch = get_current_branch()
        if not current_branch:
            click.echo("❌ 无法获取当前分支信息")
            return
        
        # 确定目标分支
        target_branch = branch or "main"
        
        # 获取git diff
        diff = get_git_diff()
        if not diff:
            click.echo("❌ 没有发现代码变更")
            return
        
        click.echo(f"🚀 正在分析推送策略...")
        click.echo(f"当前分支: {current_branch}")
        click.echo(f"目标分支: {target_branch}")
        
        # 调用API服务进行推送策略分析
        result = api_client.analyze_push_strategy(
            diff=diff,
            target_branch=target_branch,
            current_branch=current_branch,
            repository_type="github"  # 可以从配置中获取
        )
        
        if 'error' in result:
            click.echo(f"❌ 推送策略分析失败: {result['error']}")
            return
        
        # 获取推荐的提交消息
        suggested_message = result.get('commit_message', 'Auto-generated commit message')
        push_command = result.get('push_command', f'git push origin {current_branch}')
        pre_push_checks = result.get('pre_push_checks', [])
        warnings = result.get('warnings', [])
        
        # 显示推送策略
        click.echo(f"\n📋 推送策略:")
        click.echo(f"建议提交消息: {suggested_message}")
        click.echo(f"推送命令: {push_command}")
        
        # 显示预检查项
        if pre_push_checks:
            click.echo(f"\n✅ 推送前检查项:")
            for i, check in enumerate(pre_push_checks, 1):
                click.echo(f"  {i}. {check}")
        
        # 显示警告
        if warnings:
            click.echo(f"\n⚠️  警告:")
            for warning in warnings:
                click.echo(f"  • {warning}")
        
        if dry_run:
            click.echo(f"\n🏃 Dry Run模式 - 不会实际执行推送")
            return
        
        # 处理提交
        if auto_commit or not message:
            final_message = message or suggested_message
            
            # 确认提交消息
            if not auto_commit:
                if not click.confirm(f"使用建议的提交消息吗?\n消息: {final_message}"):
                    final_message = click.prompt("请输入提交消息")
            
            # 执行提交
            try:
                subprocess.run(['git', 'add', '.'], check=True)
                subprocess.run(['git', 'commit', '-m', final_message], check=True)
                click.echo(f"✅ 代码已提交: {final_message}")
            except subprocess.CalledProcessError as e:
                click.echo(f"❌ 提交失败: {e}")
                return
        
        # 确认推送
        if click.confirm(f"执行推送吗?\n命令: {push_command}"):
            try:
                # 解析推送命令并执行
                cmd_parts = push_command.split()
                subprocess.run(cmd_parts, check=True)
                click.echo("✅ 代码推送成功!")
            except subprocess.CalledProcessError as e:
                click.echo(f"❌ 推送失败: {e}")
                return
        else:
            click.echo("推送已取消")
        
    except Exception as e:
        click.echo(f"❌ 推送过程中出现错误: {str(e)}")
        raise click.ClickException(str(e)) 
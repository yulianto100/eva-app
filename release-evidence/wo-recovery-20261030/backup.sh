set -eu
cd /opt/eva
stamp=$(date +%Y%m%d-%H%M%S)
backup=/home/ubuntu/eva-backups/wo-recovery-$stamp
mkdir -p "$backup/static/js" "$backup/templates"
test "$(sha256sum static/js/app.js | cut -d' ' -f1)" = ab4969eb371d83c8a66bef61109d5c560e4093b8047459118b7d1f82bb85a469
test "$(sha256sum templates/index.html | cut -d' ' -f1)" = e68ec85edbf189025aa8b1d4cf2f71807959e2ffdfdcf2238707b3bf3acf0d61
cp -p static/js/app.js "$backup/static/js/app.js"
cp -p templates/index.html "$backup/templates/index.html"
git rev-parse HEAD > "$backup/git-head.txt"
git status --short > "$backup/git-status.txt"
git diff -- static/js/app.js templates/index.html > "$backup/prepatch.diff"
docker inspect eva-converter eva-worker eva-worker2 --format '{{.Name}} {{.Image}} {{.State.StartedAt}} {{.RestartCount}}' > "$backup/runtime.txt"
sha256sum docker-compose.yml docker-compose.override.yml > "$backup/compose-sha256.txt"
(cd "$backup" && sha256sum static/js/app.js templates/index.html > SHA256SUMS && sha256sum -c SHA256SUMS)
export GIT_INDEX_FILE="$backup/backup.index"
git read-tree HEAD
git add -- static/js/app.js templates/index.html
tree=$(git write-tree)
commit=$(printf 'backup: WO recovery production preimage\n' | git -c user.name='EVA Deploy' -c user.email='deploy@eva.dyena.id' commit-tree "$tree" -p HEAD)
branch=backup/wo-recovery-$stamp
git push origin "$commit:refs/heads/$branch"
test "$commit" = "$(git ls-remote origin "refs/heads/$branch" | cut -f1)"
for file in static/js/app.js templates/index.html; do
 test "$(git rev-parse "$commit:$file")" = "$(git hash-object "$file")"
 cmp "$backup/$file" "$file"
done
printf '%s\n' "$commit" > "$backup/remote-commit.txt"
printf '%s\n' "$branch" > "$backup/remote-branch.txt"
tar -czf "$backup/preimage.tar.gz" -C "$backup" static/js/app.js templates/index.html SHA256SUMS git-head.txt git-status.txt prepatch.diff runtime.txt compose-sha256.txt remote-commit.txt remote-branch.txt
sha256sum "$backup/preimage.tar.gz"
printf 'BACKUP_OK path=%s branch=%s commit=%s\n' "$backup" "$branch" "$commit"

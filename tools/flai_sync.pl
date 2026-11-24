#!/usr/bin/perl
# Two-way sync between ~/flai-work and the Claude Agent Hub on Railway.
# Needs only what ships with macOS (perl + curl). Never deletes anything, on either side.
#
#   perl ~/flai-work/sync.pl            sync both ways
#   perl ~/flai-work/sync.pl --dry-run  only show what would happen
#
# How it decides, per file (it remembers the last synced version in flai-work/.cloud-sync-state.json):
#   changed only on the computer -> uploaded      changed only in the cloud -> downloaded
#   changed on both sides        -> conflict: your file stays as is, the cloud version is saved next to it
#                                   as <name>.cloud-conflict-<date>.<ext>, nothing is uploaded
#   deleted on the computer      -> left alone in the cloud (not downloaded again)
# Agent instructions (SKILL.md) and settings always go from the computer to the cloud.
use strict;
use warnings;
use utf8;
use JSON::PP;
use Digest::SHA qw(sha256_hex);
use File::Find;
use File::Path qw(make_path);
use File::Temp qw(tempfile);
use File::Basename qw(dirname);
use POSIX qw(strftime);

binmode STDOUT, ':encoding(UTF-8)';
my $HUB  = $ENV{HUB_URL}   || 'https://claude-agent-hub-production.up.railway.app';
my $ROOT = $ENV{FLAI_WORK} || "$ENV{HOME}/flai-work";
my $DRY  = grep { $_ eq '--dry-run' } @ARGV;

# agent => [tools, may write shared knowledge, names on WhatsApp, Claude model]
# Model: Haiku is ~20x cheaper than Sonnet per token; '' = the CLAUDE_MODEL variable on Railway.
my %AGENTS = (
    gershon => [['code_execution'], 1, ['גרשון'], 'claude-sonnet-5-5'],
    gefen   => [['web_search', 'fetch_image'], 1, ['גפן'], 'claude-haiku-5-5'],
    tirosh  => [[], 0, ['תירוש'], 'claude-haiku-5-5'],
);
# agent => [time (Israel), message, WhatsApp number to notify or '']
my %SCHEDULES = (
    gefen => ['08:30', 'הפעל את גפן', ''],
);
my @SHARED_DIRS = ('clients', 'shared');
my %TEXT = map { $_ => 1 } qw(md txt csv);
my %BIN  = map { $_ => 1 } qw(png jpg jpeg webp gif svg pdf);
my $STATE_FILE = "$ROOT/.cloud-sync-state.json";
my $JSON = JSON::PP->new->utf8->canonical->pretty;

-d $ROOT or die "Folder not found: $ROOT\n";
my $TOKEN = $ENV{HUB_TOKEN};
unless ($TOKEN) {
    print 'HUB_TOKEN (hidden): ';
    system('stty', '-echo'); $TOKEN = <STDIN>; system('stty', 'echo'); print "\n";
    chomp $TOKEN; $TOKEN =~ s/^\s+|\s+$//g;
}

sub urlpath { my $p = shift; utf8::encode($p) if utf8::is_utf8($p); $p =~ s/([^A-Za-z0-9\/._~-])/sprintf('%%%02X', ord $1)/ge; $p }

# curl wrapper: returns (http_code, response_bytes)
sub api {
    my ($method, $path, %o) = @_;
    my ($ofh, $out) = tempfile(UNLINK => 1); close $ofh;
    my @args = ('curl', '-s', '-o', $out, '-w', '%{http_code}', '-X', $method, $HUB . $path,
                '-H', "Authorization: Bearer $TOKEN");
    if (defined $o{body}) {
        my ($bfh, $bfile) = tempfile(UNLINK => 1); binmode $bfh; print $bfh $o{body}; close $bfh;
        push @args, '-H', 'Content-Type: ' . ($o{type} || 'application/json'), '--data-binary', "\@$bfile";
    }
    open(my $pipe, '-|', @args) or die "curl failed: $!";
    my $code = <$pipe>; close $pipe;
    open(my $fh, '<:raw', $out); local $/; my $resp = <$fh> // ''; close $fh;
    return ($code // '000', $resp);
}
sub must { my ($what, $code, $resp) = @_; die "✗ $what -> $code " . substr($resp, 0, 300) . "\n" unless $code eq '200'; $resp }
sub slurp { open(my $fh, '<:raw', $_[0]) or die "$_[0]: $!"; local $/; my $d = <$fh>; close $fh; $d }
sub spew { my ($f, $d) = @_; make_path(dirname($f)); open(my $fh, '>:raw', $f) or die "$f: $!"; print $fh $d; close $fh }
# By extension, not by running `file`: the old version escaped the path (\Q...\E inside quotes), `file` could not
# open it, and its error text was uploaded as the file type, so images reached WhatsApp as unreadable documents.
my %MIME = (png => 'image/png', jpg => 'image/jpeg', jpeg => 'image/jpeg', webp => 'image/webp', gif => 'image/gif',
            svg => 'image/svg+xml', pdf => 'application/pdf');
sub mime_of { my ($ext) = $_[0] =~ /\.([^.\/]+)$/; $MIME{ lc($ext // '') } || 'application/octet-stream' }

# ---------- 1. Agent instructions and settings: computer -> cloud ----------
for my $agent (sort keys %AGENTS) {
    my $skill = "$ROOT/Agents/$agent/plugin/skills/$agent/SKILL.md";
    unless (-f $skill) { print "! $agent: no SKILL.md, skipped\n"; next }
    my $text = slurp($skill); utf8::decode($text);
    my ($tools, $shared, $names, $model) = @{ $AGENTS{$agent} };
    my $body = JSON::PP->new->utf8->encode({ instructions => $text, tools => $tools,
        shared_write => $shared ? JSON::PP::true : JSON::PP::false, names => $names, model => ($model || undef) });
    must("agent $agent", api('PUT', "/agents/$agent", body => $body)) unless $DRY;
    print "✓ $agent (" . join(',', @$names) . ', tools=[' . join(',', @$tools) . '], model=' . ($model || 'default') . ")\n";
    if (my $s = $SCHEDULES{$agent}) {
        my $sb = JSON::PP->new->utf8->encode({ at => $s->[0], message => $s->[1], notify => ($s->[2] || undef) });
        must("schedule $agent", api('PUT', "/agents/$agent/schedule", body => $sb)) unless $DRY;
        print "  ⏰ every day at $s->[0]\n";
    }
}

# ---------- 2. Knowledge and files: both ways ----------
my $kind = sub { my ($k) = @_; my ($ext) = $k =~ /\.([^.\/]+)$/; $ext = lc($ext // '');
                 $TEXT{$ext} ? 'knowledge' : $BIN{$ext} ? 'file' : undef };
my $scope_of = sub { my ($k) = @_; $k =~ m{^Agents/([^/]+)/} && $AGENTS{$1} ? $1 : '_shared' };

# local files we sync
my (%L, @SKIPPED);
my @roots = ((map { "$ROOT/Agents/$_" } sort keys %AGENTS), map { "$ROOT/$_" } @SHARED_DIRS);
for my $dir (grep { -d } @roots) {
    find({ no_chdir => 1, wanted => sub {
        if (-d $_ && m{/(plugin|\.claude-plugin)$}) { $File::Find::prune = 1; return }
        return unless -f $_;
        (my $key = $_) =~ s{^\Q$ROOT\E/}{};
        utf8::decode($key);
        return if $key =~ /\.cloud-conflict-/ || $key =~ m{(^|/)\.};
        my $k = $kind->($key) or return;
        my $data = slurp($_);
        return if $k eq 'knowledge' && $data !~ /\S/;
        if ($k eq 'file' && length($data) > 20 * 1024 * 1024) { push @SKIPPED, "$key (over 20MB)"; return }
        if ($k eq 'knowledge') { my $t = $data; utf8::decode($t); if (length($t) > 30000) { push @SKIPPED, "$key (over 30,000 characters)"; return } }
        $L{$key} = { path => $_, sha => sha256_hex($data), kind => $k, data => $data };
    } }, $dir);
}

# cloud manifest (only the agents above + shared)
my $man = decode_json(must('manifest', api('GET', '/sync/manifest')));
my %C;
for my $type (['knowledge', $man->{knowledge}], ['file', $man->{files}]) {
    for my $e (@{ $type->[1] }) {
        next unless $e->{scope} eq '_shared' || $AGENTS{ $e->{scope} };
        next unless $kind->($e->{key}) && $kind->($e->{key}) eq $type->[0];
        $C{ $e->{key} } = { sha => $e->{sha256}, scope => $e->{scope}, kind => $type->[0] };
    }
}

my $S = -f $STATE_FILE ? decode_json(slurp($STATE_FILE)) : {};
my (%newS, @report);
my $today = strftime('%Y-%m-%d', localtime);

sub push_one {
    my ($key, $l) = @_;
    my $scope = $scope_of->($key);
    return if $DRY;
    if ($l->{kind} eq 'knowledge') {
        my $text = $l->{data}; utf8::decode($text);
        must("upload $key", api('PUT', "/agents/$scope/knowledge/" . urlpath($key),
                                body => JSON::PP->new->utf8->encode({ value => $text })));
    } else {
        must("upload $key", api('PUT', "/agents/$scope/files/" . urlpath($key), body => $l->{data}, type => mime_of($l->{path})));
    }
}
sub pull_one {
    my ($key, $c, $target) = @_;
    my $what = $c->{kind} eq 'knowledge' ? 'knowledge' : 'files';
    my $data = must("download $key", api('GET', "/agents/$c->{scope}/$what/" . urlpath($key)));
    my $path = $target // "$ROOT/$key";
    utf8::encode($path) if utf8::is_utf8($path);
    spew($path, $data) unless $DRY;
    return sha256_hex($data);
}

my %keys = map { $_ => 1 } (keys %L, keys %C);
for my $key (sort keys %keys) {
    my ($l, $c, $s) = ($L{$key}, $C{$key}, $S->{$key});
    if ($l && $c && $l->{sha} eq $c->{sha}) { $newS{$key} = $l->{sha}; next }
    if ($l && !$c) {
        push_one($key, $l); $newS{$key} = $l->{sha}; push @report, "↑ $key (new)"; next;
    }
    if ($c && !$l) {
        if ($s && $s eq $c->{sha}) { $newS{$key} = $s; push @report, "· $key (deleted on the computer, kept in the cloud)"; next }
        $newS{$key} = pull_one($key, $c); push @report, "↓ $key" . ($s ? '' : ' (new)'); next;
    }
    # both exist and differ
    if ($s && $s eq $l->{sha}) { $newS{$key} = pull_one($key, $c); push @report, "↓ $key (updated in the cloud)"; next }
    if ($s && $s eq $c->{sha}) { push_one($key, $l); $newS{$key} = $l->{sha}; push @report, "↑ $key (updated on the computer)"; next }
    (my $conflict = $key) =~ s/(\.[^.\/]+)$/.cloud-conflict-$today$1/;
    pull_one($key, $c, "$ROOT/$conflict");
    $newS{$key} = $c->{sha};  # next run uploads your version, so merge from the conflict file first if needed
    push @report, "⚠ $key: changed on both sides. Yours stays; the cloud version is in $conflict. "
                . "Merge what you need into your file; the next sync uploads your file.";
}

spew($STATE_FILE, $JSON->encode(\%newS)) unless $DRY;
print @report ? "\n" . join("\n", @report) . "\n" : "\nEverything is already in sync.\n";
print "\nNot synced:\n" . join("\n", map { "   - $_" } @SKIPPED) . "\n" if @SKIPPED;
print "\n(dry run: nothing was changed)\n" if $DRY;
print "Done.\n";

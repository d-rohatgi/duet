// JavaScript for Automation. All delete targets are tracks OF one user playlist.
// JSON arguments live in a private temporary file, never interpolated as code.
ObjC.import('Foundation');

function run(argv) {
    const raw = $.NSString.stringWithContentsOfFileEncodingError(argv[0], $.NSUTF8StringEncoding, null);
    const request = JSON.parse(ObjC.unwrap(raw));
    const music = Application('com.apple.Music');
    function findPlaylist() {
        let found = request.id
            ? music.userPlaylists.whose({persistentID: request.id})()
            : music.userPlaylists.whose({name: request.name})();
        if (request.marker) found = found.filter(p => p.description() === request.marker);
        if (found.length !== 1) throw new Error('Expected exactly one matching Music playlist. Enable Sync Library and wait for it to appear.');
        if (found[0].smart()) throw new Error('Smart playlists cannot be synced.');
        return found[0];
    }
    function snapshot(playlist) {
        return {id: playlist.persistentID(), name: playlist.name(), description: playlist.description(), tracks: playlist.tracks().map(t => ({
            id: t.persistentID(), title: t.name(), artist: t.artist(), album: t.album(),
            duration_ms: Math.round(t.duration() * 1000), explicit: null
        }))};
    }
    const playlist = findPlaylist();
    if (request.action === 'snapshot') return JSON.stringify(snapshot(playlist));
    if (request.action !== 'edit') throw new Error('Unsupported command');
    const before = snapshot(playlist);
    const signature = s => JSON.stringify([s.name, s.tracks.map(t => t.id).sort()]);
    if (signature(before) !== signature(request.expected)) throw new Error('Music playlist changed before editing; retry sync.');
    const removals = new Set(request.remove_ids);
    // Iterate backwards so deleting an occurrence never shifts later targets.
    for (let index = before.tracks.length - 1; index >= 0; index--) {
        if (removals.has(before.tracks[index].id)) {
            const track = playlist.tracks[index];
            if (track.persistentID() !== before.tracks[index].id) throw new Error('Track order changed during edit.');
            music.delete(track);
        }
    }
    if (request.rename !== null) playlist.name = request.rename;
    return JSON.stringify(snapshot(playlist));
}

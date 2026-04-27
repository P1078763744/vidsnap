{ pkgs }: {
  deps = [
    pkgs.python311
    pkgs.ffmpeg
    pkgs.python311Packages.pip
  ];
}

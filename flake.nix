{
  description = "Duplicate Finder — find and manage duplicate files (PySide6 GUI)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
  };

  outputs =
    { self
    , nixpkgs
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      # Everything per system: the app package, the dev shell, the formatter.
      perSystem =
        system:
        let
          pkgs = import nixpkgs { inherit system; };

          version = "1.0.0";

          # The PEP 517 build hook expects pyproject.toml at the source root,
          # but in this repo the metadata lives at the root while the single
          # Python module (dupe_finder.py, identical in linux/ and windows/)
          # lives under linux/. Assemble a minimal source tree for the build.
          appSrc = pkgs.runCommand "dupe-finder-src" { } ''
            mkdir -p $out
            cp ${./pyproject.toml} $out/pyproject.toml
            cp ${./linux/dupe_finder.py} $out/dupe_finder.py
            cp ${./LICENSE} $out/LICENSE
          '';

          # Tesseract with English language data only (the top-level
          # `tesseract` package ships every language, ~450 MB). The wrapper
          # derivation sets TESSDATA_PREFIX itself, so no env setup is needed.
          tesseractEng = pkgs.callPackage (
            nixpkgs + "/pkgs/applications/graphics/tesseract/wrapper.nix"
          ) {
            tesseractBase = pkgs.callPackage (
              nixpkgs + "/pkgs/applications/graphics/tesseract/tesseract5.nix"
            ) { };
            languages = (pkgs.callPackage (
              nixpkgs + "/pkgs/applications/graphics/tesseract/languages.nix"
            ) { }).v4;
            enableLanguages = [ "eng" ];
          };

          dupeFinder = pkgs.python3Packages.buildPythonApplication {
            pname = "dupe-finder";
            inherit version;
            src = appSrc;
            pyproject = true;

            # PEP 517 build backend (setuptools) for the wheel build.
            build-system = with pkgs.python3Packages; [ setuptools ];

            dependencies = with pkgs.python3Packages; [
              pyside6
              pillow
            ];

            doCheck = false;

            # The app shells out to ffmpeg/ffprobe, poppler-utils and
            # tesseract at runtime; put them on PATH.
            nativeBuildInputs = [ pkgs.makeWrapper ];
            postFixup = ''
              wrapProgram "$out/bin/dupe-finder" \
                --prefix PATH : "${pkgs.lib.makeBinPath [
                  pkgs.ffmpeg
                  pkgs.poppler_utils
                  tesseractEng
                ]}"
            '';

            meta = {
              description = "Duplicate file manager for pictures, videos and documents";
              homepage = "https://github.com/Dimensionalexpressions/duplicate-finder";
              license = {
                shortName = "FUAL-1.0";
                fullName = "Free Use Attribution License 1.0 (see LICENSE)";
                free = true;
                redistributable = true;
                deprecated = false;
              };
              platforms = [
                "x86_64-linux"
                "aarch64-linux"
                "x86_64-darwin"
                "aarch64-darwin"
              ];
              mainProgram = "dupe-finder";
            };
          };
        in
        {
          package = dupeFinder;
          devShell = pkgs.mkShell {
            packages = with pkgs; [
              ffmpeg
              poppler_utils
              tesseractEng
              (python3.withPackages (
                ps: with ps; [
                  pyside6
                  pillow
                  pyinstaller
                ]
              ))
            ];
          };
        };
    in
    {
      packages = nixpkgs.lib.genAttrs systems (
        system:
        let
          p = perSystem system;
        in
        {
          dupe-finder = p.package;
          default = p.package;
        }
      );

      devShells = nixpkgs.lib.genAttrs systems (
        system: { default = (perSystem system).devShell; }
      );
    };
}

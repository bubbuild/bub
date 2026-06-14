{
  description = "bub — a common shape for agents that live alongside people";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      uv2nix,
      pyproject-nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      pyproject = builtins.fromTOML (builtins.readFile ./pyproject.toml);
      bubVersion = pyproject.tool.hatch.version.fallback-version;

      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      pyprojectOverrides = _final: prev: {
        bub = prev.bub.overrideAttrs (old: {
          env = (old.env or { }) // {
            HATCH_VCS_PRETEND_VERSION = bubVersion;
            SETUPTOOLS_SCM_PRETEND_VERSION = bubVersion;
          };
        });
      };

      pythonFor =
        pkgs:
        lib.head (pyproject-nix.lib.util.filterPythonInterpreters {
          inherit (workspace) requires-python;
          inherit (pkgs) pythonInterpreters;
        });

      mkPythonSet =
        pkgs:
        (pkgs.callPackage pyproject-nix.build.packages {
          python = pythonFor pkgs;
        }).overrideScope
          (lib.composeManyExtensions [
            pyproject-build-systems.overlays.default
            overlay
            pyprojectOverrides
          ]);

      bubFor =
        pkgs:
        ((mkPythonSet pkgs).mkVirtualEnv "bub-env" workspace.deps.default).overrideAttrs (old: {
          meta = (old.meta or { }) // {
            description = "A common shape for agents that live alongside people";
            homepage = "https://bub.build";
            license = lib.licenses.asl20;
            mainProgram = "bub";
          };
        });

      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = lib.genAttrs systems;
    in
    {
      overlays.default = final: _prev: {
        bub = bubFor final;
      };

      packages = forAllSystems (
        system:
        let
          bub = bubFor nixpkgs.legacyPackages.${system};
        in
        {
          default = bub;
          inherit bub;
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/bub";
          meta.description = "Run the bub CLI";
        };
        bub = self.apps.${system}.default;
      });

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pythonFor pkgs;
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.uv
            ];
            env =
              {
                UV_PYTHON_DOWNLOADS = "never";
                UV_PYTHON = python.interpreter;
              }
              // lib.optionalAttrs pkgs.stdenv.isLinux {
                LD_LIBRARY_PATH = lib.makeLibraryPath pkgs.pythonManylinuxPackages.manylinux1;
              };
            shellHook = ''
              unset PYTHONPATH
            '';
          };

          locked = pkgs.mkShell {
            packages = [
              (bubFor pkgs)
              pkgs.uv
            ];
            shellHook = ''
              unset PYTHONPATH
            '';
          };
        }
      );

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt-rfc-style);
    };
}

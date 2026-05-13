{ lib, pkgs, ... }:

let
  helper = import ./foo.nix { inherit lib; };
in {
  environment.systemPackages = [ pkgs.hello ];

  services.myservice = {
    enable = true;
    greeting = helper.greeting;
  };
}
